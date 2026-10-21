"""Task scheduler: spawns one engine_wrapper subprocess per task shortly before its stage opens.

* secrets are decrypted only at spawn time and written to the child's stdin pipe (never argv/env/disk)
* each child runs in its own session -> survives a bot restart; on restart we re-attach by /proc lookup
* child stdout -> per-task log file (engine already masks secrets); final line OSH_RESULT {...}
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time

from . import ui

log = logging.getLogger("runner")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(ROOT, "service", "engine_wrapper.py")


def parse_result(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode(errors="replace").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if line.startswith("OSH_RESULT "):
            try:
                return json.loads(line[11:])
            except ValueError:
                return None
    return None


def last_lines(path, n=6):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            return [l for l in f.read().decode(errors="replace").splitlines() if l.strip()][-n:]
    except FileNotFoundError:
        return []


def pid_is_task(pid, tid):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    s = [c.decode(errors="replace") for c in cmd]
    return any(x.endswith("engine_wrapper.py") for x in s) and "--task-id" in s and s[s.index("--task-id") + 1] == str(tid)


REASONS = {
    "wallet_limit": "این ولت به سقف مینتِ هر ولت در این مرحله رسیده است",
    "not_eligible": "این ولت در allowlist این مرحله نیست",
    "rpc_unavailable": "اتصال به شبکه برقرار نشد؛ دوباره تلاش کنید",
    "bad_input": "خطای داخلی (به ادمین گزارش شد)",
    "no_transfer_in_receipt": "تراکنش ثبت شد ولی NFTی به این ولت منتقل نشد",
    "sold_out": "ظرفیت کالکشن تمام شده است",
    "insufficient_funds": "موجودی ولت برای قیمت مینت کافی نیست",
    "price_changed": "قیمت مرحله تغییر کرد و طبق تنظیم قیمت شما مینت انجام نشد (چیزی ارسال نشد)",
}
HINTS = (
    ("does not cover mint price", "موجودی ولت برای قیمت مینت کافی نیست"),
    ("too low for safe gas", "موجودی ولت برای گس کافی نیست"),
    ("stage price", "قیمت مرحله با قیمتی که تأیید کرده بودید فرق دارد؛ چیزی ارسال نشد"),
    ("on-chain public price", "قیمت روی زنجیره با قیمت تأییدشده فرق دارد؛ چیزی ارسال نشد"),
    ("not found in API", "این مرحله دیگر در OpenSea وجود ندارد"),
    ("wrong chain", "شبکهٔ RPC اشتباه است"),
    ("contract account", "ولت قرارداد هوشمند است"),
    ("stage ended without", "مرحله تمام شد و OpenSea امضا نداد"),
    ("NOT on the", "این ولت در allowlist این مرحله نیست"),
    ("REFUSED", "پاسخ OpenSea با شرایط تأییدشده جور نبود؛ برای امنیت چیزی ارسال نشد"),
    ("REVERTED", "تراکنش روی زنجیره رد شد"),
    ("stage never became active", "مرحله فعال نشد"),
)


def speed_text(sp):
    """Human speed report. Times are measured from the stage start (T0) on our NTP-synced clock; block_delta is
    chain time (1 s resolution on Robinhood Chain); rank counts SeaDrop mint txs of this NFT since T0."""
    if not sp:
        return ""
    if "late_start" in sp:
        out = [f"\n⚡ سرعت ربات (تسک {sp['late_start']:.0f} ث بعد از شروع مرحله ثبت/اجرا شد؛ از لحظهٔ شلیک):"]
        for k, lab in (("own_api_win", "امضا/calldata از OpenSea"), ("own_sent", "ارسال تراکنش (پذیرش sequencer)"),
                       ("own_receipt", "تأیید روی زنجیره (رسید)")):
            if k in sp:
                out.append(f"  • {lab}: {sp[k]:+.3f} ث")
        if "block" in sp:
            out.append(f"  • بلاک {sp['block']} (ردیف {sp.get('tx_index', '?')})")
        if "rank" in sp:
            out.append(f"  • رتبه بین مینت‌های این کالکشن از شروع مرحله: #{sp['rank']} (به‌خاطر ثبت دیر، معیار رقابت نیست)")
        if "unit_price" in sp:
            out.append(f"  • قیمت پرداختی هر عدد: {sp['unit_price'] / 1e18:.6f} ETH")
        return "\n".join(out)
    out = ["\n⚡ سرعت (از لحظهٔ شروع مرحله):"]
    if "api_win" in sp:
        out.append(f"  • امضا/calldata از OpenSea: {sp['api_win']:+.3f} ث")
    if "sent" in sp:
        out.append(f"  • ارسال تراکنش (پذیرش sequencer): {sp['sent']:+.3f} ث")
    if "receipt" in sp:
        out.append(f"  • تأیید روی زنجیره (رسید): {sp['receipt']:+.3f} ث")
    if "block" in sp:
        out.append(f"  • بلاک {sp['block']} (ردیف {sp.get('tx_index', '?')})"
                   + (f"، زمان بلاک {sp['block_delta']:+d} ث نسبت به شروع" if "block_delta" in sp else ""))
    if "rank" in sp:
        out.append(f"  • 🏁 رتبهٔ ما بین مینت‌های این کالکشن از لحظهٔ شروع: #{sp['rank']}")
    if "unit_price" in sp:
        out.append(f"  • قیمت پرداختی هر عدد: {sp['unit_price'] / 1e18:.6f} ETH")
    return "\n".join(out)


def result_card(t, status, detail, res=None):
    """Rich RTL notification card (service.rich.Doc) + inline buttons for a finished task.
    `detail` is the plain-text result (also stored in the DB); for a mint the card is built from the structured result."""
    from . import rich as R
    from .rich import Doc, lt, ltr, mono
    res = res or {}
    head = {"minted": ("party", "مینت موفق!"), "failed": ("no", "مینت انجام نشد"), "ended": ("wait", "مرحله تمام شد"),
            "cancelled": ("stop", "تسک لغو شد"), "not_eligible": ("stop", "خارج از allowlist")}.get(status, ("info", "نتیجهٔ تسک"))
    d = Doc().h(head[1] + " " + ltr(f"#{t['id']}"), head[0], 2)
    rows = [("کالکشن", lt(t["slug"])), ("مرحله", lt(t["stage_label"]))]
    tx = res.get("tx")
    if status == "minted":
        ids = res.get("token_ids") or []
        n, want = len(ids), res.get("wanted") or 1
        rows.append(("تعداد", f"<b>{ltr(n)}</b>" + (f" از {ltr(want)} درخواستی" if n < want else "")))
        if ids:
            shown = ", ".join(f"#{i}" for i in ids[:12]) + (" …" if len(ids) > 12 else "")
            rows.append(("توکن", ltr(shown)))
        sp = res.get("speed") or {}
        if "unit_price" in sp:
            rows.append(("قیمت پرداختی هر عدد", R.eth(sp["unit_price"])))
        if tx:
            rows.append(("تراکنش", mono(tx[:10] + "…" + tx[-6:])))
        d.kv(rows)
        d.p(R.esc(detail.splitlines()[0]) if detail else "")        # «مینت انجام شد — N عدد …» (plain summary line)
        speed_doc(d, sp)
    else:
        d.kv(rows)
        body = "\n".join(l for l in (detail or "").splitlines() if not l.startswith("tx: "))
        d.note(R.esc(body), "warn" if status in ("failed", "not_eligible") else "info")
    d.footer("Ray Minter · Robinhood Chain")
    mk = []
    if tx:
        mk.append([ui.btn("مشاهده تراکنش", url=ui.tx_url(tx), icon="search"), ui.btn("کپی tx", copy=tx, icon="link")])
    if status == "minted":
        mk.append([ui.btn("کالکشن در OpenSea", url=f"https://opensea.io/collection/{t['slug']}", icon="gem")])
    return d, ui.kb(*mk) if mk else None


def speed_doc(d, sp):
    """Speed report as a table (same numbers as speed_text)."""
    from .rich import lt, ltr
    if not sp:
        return d
    late = "late_start" in sp
    pre = "own_" if late else ""
    rows = [[lab, ltr(f"{sp[pre + k]:+.3f} s")] for k, lab in (
        ("api_win", f"امضا/calldata از {lt('OpenSea')}"), ("sent", f"ارسال تراکنش (پذیرش {lt('sequencer')})"),
        ("receipt", "تأیید روی زنجیره (رسید)")) if pre + k in sp]
    if "block" in sp:
        b = f"{ltr(sp['block'])} · ردیف {ltr(sp.get('tx_index', '?'))}"
        if not late and "block_delta" in sp:
            b += f" · زمان بلاک {ltr(format(sp['block_delta'], '+d') + ' s')}"
        rows.append(["بلاک", b])
    if "rank" in sp:
        rows.append(["رتبهٔ ما" if not late else "رتبه", f"<b>{ltr('#' + str(sp['rank']))}</b>"
                     + (" — معیار رقابت نیست" if late else "")])
    late_s = f"{sp['late_start']:.0f}" if late else ""
    cap = (f"⚡ سرعت ربات — از لحظهٔ شلیک (تسک {ltr(late_s)} ث بعد از شروع مرحله اجرا شد)" if late
           else "⚡ سرعت — از لحظهٔ شروع مرحله")
    d.table(["گام", "زمان"], rows, caption=cap)
    if late:
        d.note("تسک بعد از شروع مرحله ثبت شد؛ این گزارش سرعت خود ربات است، نه رقابت از لحظهٔ شروع.", "info")
    return d


def classify(res, log_tail=""):
    """-> (status, user message). Raw Python errors are never shown to the user."""
    if not res:
        return "failed", "پروسهٔ مینت بدون نتیجه تمام شد (به ادمین گزارش شد)"
    if res.get("minted"):
        ids = res.get("token_ids") or []
        n, want = len(ids), res.get("wanted") or 1
        m = f"مینت انجام شد — {n} عدد" + (f" (از {want} درخواستی؛ سقف ولت/موجودی/عرضه اجازهٔ بیشتر نداد)" if n < want else "")
        if ids:
            shown = ids if len(ids) <= 12 else ids[:12]
            m += "\nتوکن: #" + ", #".join(map(str, shown)) + (" …" if len(ids) > 12 else "")
        m += speed_text(res.get("speed") or {})
        return "minted", m + (f"\ntx: {res['tx']}" if res.get("tx") else "")
    reason = res.get("reason")
    if reason in REASONS:
        return ("not_eligible" if reason == "not_eligible" else "failed"), REASONS[reason]
    text = (res.get("error") or "") + "\n" + (log_tail or "")
    for needle, msg in HINTS:
        if needle in text:
            return "failed", msg
    return "failed", "مینت انجام نشد (به ادمین گزارش شد)"


class Runner:
    def __init__(self, db, vault, cfg, notify, extra_args=None, admin_notify=None, notify_rich=None):
        self.db, self.vault, self.cfg, self.notify = db, vault, cfg, notify
        self.admin_notify = admin_notify
        self.notify_rich = notify_rich               # fn(uid, html, markup, effect) -> formatted cards (bot)
        self.extra_args = list(extra_args or [])    # tests only (fork RPC / fake OpenSea)
        self.procs = {}                              # tid -> Popen (only for children we spawned)
        os.makedirs(cfg.log_dir, mode=0o700, exist_ok=True)
        self._warned = set()

    def log_path(self, tid):
        return os.path.join(self.cfg.log_dir, f"task-{tid}.log")

    def task_key(self, t):
        """The task's own key if still valid through the stage start; else the least-loaded other valid key."""
        uid = t["user_id"]
        k = self.db.apikey_by_id(uid, t["apikey_id"]) if t["apikey_id"] else None
        if k and (not k["expires_at"] or k["expires_at"] > max(time.time(), t["start_ts"]) + 300):
            return k
        it = {"start": t["start_ts"], "end": t["end_ts"]}
        kid, _ = self.db.pick_keys(uid, [it])[0]
        return self.db.apikey_by_id(uid, kid) if kid else None

    def nonce_path(self, wid):
        """One lock file per wallet: sibling tasks (other stages of the same wallet) coordinate nonces through it."""
        return os.path.join(self.cfg.log_dir, f"wallet-{int(wid)}.nonce")

    # ------------------------------------------------------------------ spawn / cancel
    def spawn(self, t):
        uid, tid = t["user_id"], t["id"]
        w = self.db.wallet(uid, t["wallet_id"])
        k = self.task_key(t)
        if not w or not k:
            return self._finish(t, "failed", "ولت یا کلید API حذف شده است")
        try:
            pk = self.vault.decrypt(uid, w["address"], w["blob"])
            api_key = self.vault.open(uid, "apikey", k["blob"])
            if k["id"] != t["apikey_id"]:
                self.db.update_task(tid, apikey_id=k["id"])
                log.info("task %s: key %s -> %s (assigned key gone/expiring)", tid, t["apikey_id"], k["id"])
        except Exception as e:  # noqa: BLE001
            return self._finish(t, "failed", f"خطای رمزگشایی: {e}")
        cmd = [sys.executable, WRAPPER, "--task-id", str(tid), "--slug", t["slug"], "--nft", t["nft"],
               "--label", t["stage_label"], "--start", str(t["start_ts"]), "--end", str(t["end_ts"]),
               "--kind", t["kind"], "--price-wei", str(t["price_wei"]), "--max-value-wei", str(t["max_value_wei"]),
               "--quantity", str(t["quantity"]), "--nonce-file", self.nonce_path(t["wallet_id"])]
        if t["stage_uuid"]:
            cmd += ["--stage-uuid", t["stage_uuid"]]
        if t["min_unit_wei"] is not None:
            cmd += ["--min-unit-wei", str(t["min_unit_wei"])]
        if t["max_unit_wei"] is not None:
            cmd += ["--max-unit-wei", str(t["max_unit_wei"])]
        cmd += self.extra_args
        fd = os.open(self.log_path(tid), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fd, stderr=subprocess.STDOUT,
                                 cwd=ROOT, start_new_session=True, close_fds=True)
        finally:
            os.close(fd)
        try:
            p.stdin.write((json.dumps({"pk": pk, "api_key": api_key}) + "\n").encode())
            p.stdin.close()
        finally:
            del pk, api_key
        self.procs[tid] = p
        self.db.update_task(tid, status="running", pid=p.pid)
        self.db.audit(uid, "task_spawn", f"task {tid} pid {p.pid}")
        log.info("spawned task %s pid %s", tid, p.pid)

    def cancel(self, uid, tid):
        t = self.db.task(tid)
        if not t or t["user_id"] != uid or t["status"] not in ("scheduled", "running"):
            return False
        if t["status"] == "running" and t["pid"] and pid_is_task(t["pid"], tid):
            try:
                os.killpg(t["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.db.update_task(tid, status="cancelled", result="لغو توسط کاربر")
        self.db.audit(uid, "task_cancel", str(tid))
        self.procs.pop(tid, None)
        return True

    def _finish(self, t, status, detail):
        res = parse_result(self.log_path(t["id"])) or {}
        self.db.update_task(t["id"], status=status, result=detail[:900], tx_hash=res.get("tx"))
        self.procs.pop(t["id"], None)
        icon = {"minted": "✅", "failed": "❌", "ended": "⌛", "cancelled": "🚫", "not_eligible": "⛔"}.get(status, "ℹ️")
        if self.notify_rich:
            txt, mk = result_card(t, status, detail, res)
            self.notify_rich(t["user_id"], txt, mk, ui.EFFECT_PARTY if status == "minted" else None)
        else:
            self.notify(t["user_id"], f"{icon} تسک #{t['id']} ({t['slug']} / {t['stage_label']}): {detail}")
        if status not in ("minted", "cancelled") and self.admin_notify:      # raw log goes to admins only
            tail = "\n".join(last_lines(self.log_path(t["id"]), 8))
            self.admin_notify(f"[task #{t['id']} user {t['user_id']} -> {status}] {detail}\n{tail[-1500:]}")

    # ------------------------------------------------------------------ main loop
    def tick(self, now=None):
        now = now or time.time()
        for t in self.db.active_tasks():
            tid = t["id"]
            if t["status"] == "scheduled":
                if now > t["end_ts"]:
                    self._finish(t, "ended", "مرحله قبل از اجرا تمام شد")
                    continue
                lead = t["start_ts"] - now
                if 0 < lead - 86400 < 60 and (tid, "24h") not in self._warned:
                    self._warned.add((tid, "24h"))
                    self._pre_warn(t)
                if lead <= self.cfg.spawn_before_s:
                    if not self.db.is_subscribed(t["user_id"], now):
                        self._finish(t, "failed", "اشتراک منقضی شده است؛ تمدید کنید و تسک را دوباره بسازید")
                        continue
                    if not self.task_key(t):
                        self._finish(t, "failed", "هیچ کلید API معتبری تا زمان شروع مرحله ندارید؛ کلید جدید ثبت کنید")
                        continue
                    self.spawn(t)
            elif t["status"] == "running":
                p = self.procs.get(tid)
                alive = (p.poll() is None) if p else (bool(t["pid"]) and pid_is_task(t["pid"], tid))
                if alive:
                    continue
                st, detail = classify(parse_result(self.log_path(tid)), "\n".join(last_lines(self.log_path(tid), 40)))
                self._finish(t, st, detail)

    def _pre_warn(self, t):
        if not self.task_key(t):
            msg = (f"⚠️ کلید API شما قبل از پایان مرحلهٔ تسک #{t['id']} منقضی می‌شود. "
                   "لطفاً از منوی «🔑 کلید API» کلید جدید ثبت کنید.")
            if self.notify_rich:
                from .rich import Doc, ltr
                doc = (Doc().h(f"هشدار کلید {ltr('API')}", "warn", 3)
                       .p(f"کلید {ltr('API')} شما قبل از پایان مرحلهٔ تسک {ltr('#' + str(t['id']))} منقضی می‌شود.")
                       .note("از منوی «🔑 کلید API» یا دکمهٔ زیر کلید جدید ثبت کنید.", "lock"))
                self.notify_rich(t["user_id"], doc,
                                 ui.kb([ui.btn("افزودن کلید API", "kset", style="success", icon="plus")]), None)
            else:
                self.notify(t["user_id"], msg)

    async def loop(self, period=1.0):
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.tick)
            except Exception:  # noqa: BLE001
                log.exception("runner tick failed")
            await asyncio.sleep(period)
