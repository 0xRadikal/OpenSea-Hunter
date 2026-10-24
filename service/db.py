"""SQLite storage. All secrets (wallet keys, OpenSea API keys) are stored only as vault ciphertext."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, created INTEGER NOT NULL,
  sub_until INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wallets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  address TEXT NOT NULL, blob BLOB NOT NULL, created INTEGER NOT NULL, UNIQUE(user_id, address));
CREATE TABLE IF NOT EXISTS apikeys(
  user_id INTEGER PRIMARY KEY REFERENCES users(id), blob BLOB NOT NULL,
  expires_at INTEGER, source TEXT NOT NULL, created INTEGER NOT NULL, last_ok INTEGER);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  wallet_id INTEGER NOT NULL REFERENCES wallets(id), slug TEXT NOT NULL, nft TEXT NOT NULL,
  stage_label TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
  status TEXT NOT NULL, pid INTEGER, log_offset INTEGER NOT NULL DEFAULT 0,
  tx_hash TEXT, result TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'signed', price_wei TEXT NOT NULL DEFAULT '0', max_value_wei TEXT NOT NULL DEFAULT '0',
  quantity INTEGER NOT NULL DEFAULT 1, plan_id TEXT, apikey_id INTEGER,
  min_unit_wei TEXT, max_unit_wei TEXT, price_policy TEXT NOT NULL DEFAULT 'exact', stage_uuid TEXT);
CREATE TABLE IF NOT EXISTS apikeys2(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), blob BLOB NOT NULL,
  expires_at INTEGER, source TEXT NOT NULL, created INTEGER NOT NULL, label TEXT, fp TEXT NOT NULL,
  UNIQUE(user_id, fp));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_prefs(
  user_id INTEGER PRIMARY KEY REFERENCES users(id), price_policy TEXT NOT NULL DEFAULT 'exact',
  max_increase_pct INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments(
  charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, stars INTEGER NOT NULL,
  payload TEXT NOT NULL, created INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ton_invoices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, memo TEXT NOT NULL UNIQUE,
  amount_nano INTEGER NOT NULL, price_nano INTEGER NOT NULL, days INTEGER NOT NULL,
  created INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  paid_nano INTEGER NOT NULL DEFAULT 0, periods INTEGER NOT NULL DEFAULT 0, paid_at INTEGER);
CREATE INDEX IF NOT EXISTS ton_inv_open ON ton_invoices(status, expires);
CREATE TABLE IF NOT EXISTS ton_payments(
  tx_hash TEXT PRIMARY KEY, lt INTEGER NOT NULL, utime INTEGER NOT NULL, value_nano INTEGER NOT NULL,
  comment TEXT, memo TEXT, source TEXT, invoice_id INTEGER, user_id INTEGER, kind TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, user_id INTEGER, action TEXT NOT NULL, detail TEXT);
"""
ACTIVE = ("scheduled", "running")
FINAL = ("minted", "failed", "cancelled", "not_eligible", "ended")


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA)
        self.lock = threading.RLock()
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'signed'"), ("price_wei", "TEXT NOT NULL DEFAULT '0'"),
                         ("max_value_wei", "TEXT NOT NULL DEFAULT '0'"), ("quantity", "INTEGER NOT NULL DEFAULT 1"),
                         ("plan_id", "TEXT"), ("apikey_id", "INTEGER"), ("min_unit_wei", "TEXT"),
                         ("max_unit_wei", "TEXT"), ("price_policy", "TEXT NOT NULL DEFAULT 'exact'"),
                         ("stage_uuid", "TEXT")):
            if col not in cols:                     # migrate DBs created before paid/public support
                self.c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")

        self._migrate_apikeys()

    def _migrate_apikeys(self):
        """Copy legacy single-key rows (apikeys) into the key pool once; legacy table is kept untouched."""
        import hashlib
        with self.lock:
            for r in self.c.execute("SELECT * FROM apikeys").fetchall():
                fp = "legacy:" + hashlib.sha256(bytes(r["blob"])).hexdigest()[:16]
                self.c.execute("INSERT OR IGNORE INTO apikeys2(user_id,blob,expires_at,source,created,label,fp) "
                               "VALUES(?,?,?,?,?,?,?)", (r["user_id"], r["blob"], r["expires_at"], r["source"], r["created"],
                                                          "کلید ۱", fp))

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            return cur.lastrowid

    def audit(self, uid, action, detail=""):
        self.x("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)", (int(time.time()), uid, action, detail[:500]))

    # users / subscriptions
    def ensure_user(self, uid, username=None):
        # username=None (internal calls: extend_sub, payments, admin) must NOT erase a known username
        self.x("INSERT INTO users(id,username,created) VALUES(?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET username=COALESCE(excluded.username, users.username)",
               (uid, username, int(time.time())))
        return self.user(uid)

    def user(self, uid):
        r = self.q("SELECT * FROM users WHERE id=?", (uid,))
        return r[0] if r else None

    def is_subscribed(self, uid, now=None):
        u = self.user(uid)
        return bool(u) and not u["banned"] and u["sub_until"] > (now or time.time())

    def extend_sub(self, uid, days, now=None):
        """Extend from max(now, current expiry) so early renewals are never lost."""
        now = int(now or time.time())
        with self.lock:
            self.ensure_user(uid)
            cur = self.user(uid)["sub_until"]
            new = max(now, cur) + int(days * 86400)
            self.x("UPDATE users SET sub_until=? WHERE id=?", (new, uid))
            return new

    def record_payment(self, charge_id, uid, stars, payload, days):
        """Idempotent: a duplicate charge id (Telegram redelivery) never extends twice. Returns new expiry or None."""
        with self.lock:
            self.c.execute("BEGIN IMMEDIATE")       # charge row + subscription extension: all or nothing
            try:
                try:
                    self.x("INSERT INTO payments(charge_id,user_id,stars,payload,created) VALUES(?,?,?,?,?)",
                           (charge_id, uid, stars, payload, int(time.time())))
                except sqlite3.IntegrityError:
                    self.c.execute("ROLLBACK")
                    return None
                new = self.extend_sub(uid, days)
                self.c.execute("COMMIT")
                return new
            except Exception:
                self.c.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ TON payments (service/ton_pay.py)
    def ton_invoice_for(self, uid, price, days, now, ttl, memo_fn, step, max_off):
        """Reuse the user's open, unexpired invoice for the same price/period, else create one whose amount
        (price + k*step, k in 1..max_off) is unique among all open unexpired invoices."""
        with self.lock:
            r = self.q("SELECT * FROM ton_invoices WHERE user_id=? AND status='open' AND price_nano=? AND days=? "
                       "AND expires>? ORDER BY id DESC LIMIT 1", (uid, price, days, now + 300))
            if r:
                return r[0]
            taken = {x[0] for x in self.q("SELECT amount_nano FROM ton_invoices WHERE status='open' AND expires>?",
                                          (now - 86400,))}
            free = [k for k in range(1, max_off + 1) if price + k * step not in taken]
            if not free:
                raise RuntimeError("no free unique TON amount (too many open invoices)")
            amount = price + free[__import__("secrets").randbelow(len(free))] * step
            for _ in range(20):
                memo = memo_fn()
                try:
                    iid = self.x("INSERT INTO ton_invoices(user_id,memo,amount_nano,price_nano,days,created,expires) "
                                 "VALUES(?,?,?,?,?,?,?)", (uid, memo, amount, price, days, now, now + ttl))
                    return self.q("SELECT * FROM ton_invoices WHERE id=?", (iid,))[0]
                except sqlite3.IntegrityError:
                    continue
            raise RuntimeError("could not allocate a unique memo")

    def ton_invoice(self, iid):
        r = self.q("SELECT * FROM ton_invoices WHERE id=?", (iid,))
        return r[0] if r else None

    def ton_oldest_open_invoice(self):
        r = self.q("SELECT MIN(created) FROM ton_invoices WHERE status='open'")
        return r[0][0] if r and r[0][0] is not None else None

    def ton_has_open_invoices(self, since):
        return bool(self.q("SELECT 1 FROM ton_invoices WHERE status='open' AND created>? LIMIT 1", (since,)))

    def ton_apply(self, inc, now):
        """Credit one inbound transfer atomically. inc: dict from ton_pay.incoming().
        -> ('dup', {}) | ('paid', {...}) | ('partial', {...}) | ('unmatched', {...})"""
        with self.lock:
            self.c.execute("BEGIN IMMEDIATE")
            try:
                if self.q("SELECT 1 FROM ton_payments WHERE tx_hash=?", (inc["hash"],)):
                    self.c.execute("ROLLBACK")
                    return "dup", {}
                inv = None
                how = None
                if inc.get("memo"):
                    r = self.q("SELECT * FROM ton_invoices WHERE memo=?", (inc["memo"],))
                    if r and r[0]["status"] != "cancelled":
                        inv, how = r[0], "memo"
                if inv is None:
                    # no (known) memo: exact unique amount of ONE open invoice that was valid when the tx happened
                    r = self.q("SELECT * FROM ton_invoices WHERE status='open' AND amount_nano=? AND created<=? AND expires>=?",
                               (inc["value"], inc["utime"] + 120, inc["utime"]))
                    if len(r) == 1:
                        inv, how = r[0], "amount"
                if inv is None:
                    self.x("INSERT INTO ton_payments(tx_hash,lt,utime,value_nano,comment,memo,source,kind,created) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (inc["hash"], inc["lt"], inc["utime"], inc["value"], inc.get("comment"),
                                                         inc.get("memo"), inc.get("source"), "unmatched", now))
                    self.c.execute("COMMIT")
                    return "unmatched", {}
                total = inv["paid_nano"] + inc["value"]
                periods = total // inv["price_nano"]                       # several full payments -> several periods
                new_periods = periods - inv["periods"]
                until = None
                if new_periods > 0:
                    self.ensure_user(inv["user_id"])
                    until = self.extend_sub(inv["user_id"], inv["days"] * new_periods, now)
                self.x("UPDATE ton_invoices SET paid_nano=?, periods=?, status=?, paid_at=COALESCE(paid_at, ?) WHERE id=?",
                       (total, periods, "paid" if periods > 0 else inv["status"], now if periods > 0 else None, inv["id"]))
                kind = "paid" if new_periods > 0 else "partial"
                self.x("INSERT INTO ton_payments(tx_hash,lt,utime,value_nano,comment,memo,source,invoice_id,user_id,kind,created) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (inc["hash"], inc["lt"], inc["utime"], inc["value"], inc.get("comment"),
                                                         inc.get("memo"), inc.get("source"), inv["id"], inv["user_id"], kind, now))
                self.c.execute("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)",
                               (now, inv["user_id"], "ton_" + kind, f"inv {inv['id']} {inc['value']} nano by {how} tx {inc['hash']}"))
                self.c.execute("COMMIT")
                need = max(0, (inv["periods"] + 1) * inv["price_nano"] - total) if kind == "partial" else 0
                return kind, {"uid": inv["user_id"], "invoice": inv["id"], "memo": inv["memo"], "until": until,
                              "periods": new_periods, "total": total, "need": need, "how": how, "days": inv["days"]}
            except Exception:
                self.c.execute("ROLLBACK")
                raise

    def ton_record_history(self, inc, now):
        """Transfers that existed before the gateway started: stored so they are never processed again, never credited."""
        self.x("INSERT OR IGNORE INTO ton_payments(tx_hash,lt,utime,value_nano,comment,memo,source,kind,created) "
               "VALUES(?,?,?,?,?,?,?,?,?)", (inc["hash"], inc["lt"], inc["utime"], inc["value"], inc.get("comment"),
                                             inc.get("memo"), inc.get("source"), "history", now))

    def ton_assign(self, tx_hash, uid, days, now, admin):
        """Admin: credit an UNMATCHED recorded payment to a user. -> new expiry or None (unknown / already used)."""
        with self.lock:
            self.c.execute("BEGIN IMMEDIATE")
            try:
                r = self.q("SELECT kind FROM ton_payments WHERE tx_hash=?", (tx_hash,))
                if not r or r[0]["kind"] != "unmatched":
                    self.c.execute("ROLLBACK")
                    return None
                self.ensure_user(uid)
                until = self.extend_sub(uid, days, now)
                self.x("UPDATE ton_payments SET kind='manual', user_id=? WHERE tx_hash=?", (uid, tx_hash))
                self.c.execute("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)",
                               (now, uid, "ton_manual", f"tx {tx_hash} {days}d by admin {admin}"))
                self.c.execute("COMMIT")
                return until
            except Exception:
                self.c.execute("ROLLBACK")
                raise

    def ton_unmatched(self, limit=10):
        return self.q("SELECT * FROM ton_payments WHERE kind='unmatched' ORDER BY lt DESC LIMIT ?", (limit,))

    # wallets
    def add_wallet(self, uid, address, blob):
        with self.lock:
            self.x("INSERT INTO wallets(user_id,address,blob,created) VALUES(?,?,?,?) "
                   "ON CONFLICT(user_id,address) DO UPDATE SET blob=excluded.blob", (uid, address, blob, int(time.time())))
            return self.q("SELECT id FROM wallets WHERE user_id=? AND address=?", (uid, address))[0]["id"]

    def wallets(self, uid):
        return self.q("SELECT id,address,created FROM wallets WHERE user_id=? ORDER BY id", (uid,))

    def wallet(self, uid, wid):
        r = self.q("SELECT * FROM wallets WHERE user_id=? AND id=?", (uid, wid))
        return r[0] if r else None

    def max_active(self, default):
        v = self.setting("max_active_tasks")
        return int(v) if v and v.isdigit() else default

    def delete_wallet(self, uid, wid):
        with self.lock:
            if self.q("SELECT 1 FROM tasks WHERE wallet_id=? AND status IN ('scheduled','running')", (wid,)):
                return False
            self.x("DELETE FROM tasks WHERE wallet_id=? AND user_id=?", (wid, uid))
            self.x("DELETE FROM wallets WHERE id=? AND user_id=?", (wid, uid))
            return True

    # api keys (pool: each task gets its own key when the user has enough)
    MAX_KEYS = 50

    def add_apikey(self, uid, blob, expires_at, source, fp):
        """fp = sha256 fingerprint of the plaintext key (dedupe). Returns key id, "dup" or "full"."""
        with self.lock:
            if self.q("SELECT 1 FROM apikeys2 WHERE user_id=? AND fp=?", (uid, fp)):
                return "dup"
            if len(self.apikeys(uid)) >= self.MAX_KEYS:
                return "full"
            n = self.q("SELECT COUNT(*) FROM apikeys2 WHERE user_id=?", (uid,))[0][0] + 1
            return self.x("INSERT INTO apikeys2(user_id,blob,expires_at,source,created,label,fp) VALUES(?,?,?,?,?,?,?)",
                          (uid, blob, expires_at, source, int(time.time()), f"کلید {n}", fp))

    def set_apikey(self, uid, blob, expires_at, source):            # legacy single-key API (tests / old callers)
        import hashlib
        return self.add_apikey(uid, blob, expires_at, source, "blob:" + hashlib.sha256(bytes(blob)).hexdigest()[:16])

    def apikeys(self, uid, valid_after=None):
        sql = "SELECT * FROM apikeys2 WHERE user_id=?"
        args = [uid]
        if valid_after is not None:
            sql += " AND (expires_at IS NULL OR expires_at>?)"
            args.append(int(valid_after))
        return self.q(sql + " ORDER BY id", args)

    def apikey_by_id(self, uid, kid):
        r = self.q("SELECT * FROM apikeys2 WHERE user_id=? AND id=?", (uid, kid))
        return r[0] if r else None

    def apikey(self, uid):
        """Any usable key (latest-expiring) — for read calls (drop info, eligibility)."""
        r = self.q("SELECT * FROM apikeys2 WHERE user_id=? ORDER BY COALESCE(expires_at, 9e18) DESC, id DESC LIMIT 1", (uid,))
        return r[0] if r else None

    def delete_apikey(self, uid, kid):
        with self.lock:
            if self.q("SELECT 1 FROM tasks WHERE apikey_id=? AND user_id=? AND status IN ('scheduled','running')", (kid, uid)):
                return False
            self.x("DELETE FROM apikeys2 WHERE id=? AND user_id=?", (kid, uid))
            return True

    def key_load(self, uid, start, end, margin=600):
        """{key_id: number of ACTIVE tasks whose stage window overlaps [start-margin, end]}"""
        rows = self.q("SELECT apikey_id, COUNT(*) n FROM tasks WHERE user_id=? AND status IN ('scheduled','running') "
                      "AND apikey_id IS NOT NULL AND start_ts < ? AND end_ts > ? GROUP BY apikey_id",
                      (uid, end, start - margin))
        return {r["apikey_id"]: r["n"] for r in rows}

    def pick_keys(self, uid, items, margin=600):
        """Assign a key to each plan item: prefer a key with NO overlapping active task (own rate-limit budget per
        task), valid until the item's end; else the least loaded valid key. -> [(key_id|None, shared:bool)]"""
        out, taken = [], {}
        for it in items:
            keys = [k for k in self.apikeys(uid) if not k["expires_at"] or k["expires_at"] > it["start"] + 300]
            if not keys:
                out.append((None, False)); continue
            load = self.key_load(uid, it["start"], it["end"], margin)
            for kid, (_unused, e0) in taken.items():
                for s1, e1 in e0:
                    if s1 < it["end"] and e1 > it["start"] - margin:
                        load[kid] = load.get(kid, 0) + 1
            best = min(keys, key=lambda k: (load.get(k["id"], 0), -(k["expires_at"] or 9e18), k["id"]))
            out.append((best["id"], load.get(best["id"], 0) > 0))
            taken.setdefault(best["id"], (None, []))[1].append((it["start"], it["end"]))
        # (note: key_load counts ACTIVE tasks in the DB; `taken` adds the items of this plan not yet inserted)
        return out

    # settings / prefs
    def setting(self, k, default=None):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def set_setting(self, k, v):
        self.x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    PRICE_POLICIES = ("exact", "lower_ok", "cap", "any_up")

    def prefs(self, uid):
        r = self.q("SELECT * FROM user_prefs WHERE user_id=?", (uid,))
        return dict(r[0]) if r else {"user_id": uid, "price_policy": "exact", "max_increase_pct": 0}

    def set_prefs(self, uid, policy, pct=0):
        if policy not in self.PRICE_POLICIES or not 0 <= int(pct) <= 1000:
            raise ValueError("bad price policy")
        self.ensure_user(uid)
        self.x("INSERT INTO user_prefs(user_id,price_policy,max_increase_pct) VALUES(?,?,?) ON CONFLICT(user_id) DO "
               "UPDATE SET price_policy=excluded.price_policy,max_increase_pct=excluded.max_increase_pct", (uid, policy, int(pct)))

    # tasks
    def add_task(self, uid, wid, slug, nft, label, start, end, kind="signed", price_wei=0, max_value_wei=None,
                 quantity=1, plan_id=None, apikey_id=None, min_unit=None, max_unit=None, policy="exact", stage_uuid=None):
        quantity = int(quantity)
        lo = int(price_wei) if min_unit is None else int(min_unit)
        hi = int(price_wei) if max_unit is None else int(max_unit)
        if max_value_wei is None:
            max_value_wei = hi * quantity
        if kind not in ("signed", "public") or not 1 <= quantity <= 100 or int(price_wei) < 0 or not 0 <= lo <= hi \
                or hi * quantity > int(max_value_wei) or policy not in self.PRICE_POLICIES:
            raise ValueError("bad mint policy")
        now = int(time.time())
        return self.x("INSERT INTO tasks(user_id,wallet_id,slug,nft,stage_label,start_ts,end_ts,status,created,updated,"
                      "kind,price_wei,max_value_wei,quantity,plan_id,apikey_id,min_unit_wei,max_unit_wei,price_policy,stage_uuid) "
                      "VALUES(?,?,?,?,?,?,?,'scheduled',?,?,?,?,?,?,?,?,?,?,?,?)",
                      (uid, wid, slug, nft.lower(), label, start, end, now, now, kind, str(int(price_wei)),
                       str(int(max_value_wei)), quantity, plan_id, apikey_id,
                       None if min_unit is None else str(lo), None if max_unit is None else str(hi), policy, stage_uuid))

    def add_plan(self, uid, wid, slug, nft, items, max_active, plan_id):
        """Atomically insert one task per stage of a plan. items: [{label,start,end,kind,price_wei,quantity}].
        All-or-nothing. Returns [ids], "dup" (some stage already has an active task), "limit", or "nowallet"
        (the wallet was deleted after the plan was built)."""
        with self.lock:
            if not self.wallet(uid, wid):
                return "nowallet"
            if any(self.duplicate_task(uid, wid, nft, it["label"]) for it in items):
                return "dup"
            if self.active_count(uid) + len(items) > max_active:
                return "limit"
            for it in items:                                       # validate everything before writing anything
                if it["kind"] not in ("signed", "public") or not 1 <= int(it["quantity"]) <= 100 or int(it["price_wei"]) < 0:
                    raise ValueError("bad mint policy")
            keys = self.pick_keys(uid, items)
            self.c.execute("BEGIN IMMEDIATE")
            try:
                ids = []
                for it, (kid, _shared) in zip(items, keys):
                    hi = it.get("max_unit", it["price_wei"])
                    ids.append(self.add_task(uid, wid, slug, nft, it["label"], it["start"], it["end"], it["kind"],
                                             it["price_wei"], int(hi) * int(it["quantity"]), it["quantity"], plan_id,
                                             kid, it.get("min_unit"), it.get("max_unit"), it.get("policy", "exact"),
                                             it.get("uuid")))
                self.c.execute("COMMIT")
                return ids
            except Exception:
                self.c.execute("ROLLBACK")
                raise

    def add_task_once(self, uid, wid, slug, nft, label, start, end, max_active, kind="signed", price_wei=0, max_value_wei=None):  # legacy
        """Atomic duplicate/limit check + insert. Returns task id, "dup" or "limit"."""
        with self.lock:
            if self.duplicate_task(uid, wid, nft, label):
                return "dup"
            if self.active_count(uid) >= max_active:
                return "limit"
            return self.add_task(uid, wid, slug, nft, label, start, end, kind, price_wei, max_value_wei)

    def task(self, tid):
        r = self.q("SELECT * FROM tasks WHERE id=?", (tid,))
        return r[0] if r else None

    def tasks(self, uid, active_only=False):
        sql = "SELECT * FROM tasks WHERE user_id=?" + (" AND status IN ('scheduled','running')" if active_only else "")
        return self.q(sql + " ORDER BY id DESC LIMIT 20", (uid,))

    def active_count(self, uid):
        return self.q("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status IN ('scheduled','running')", (uid,))[0][0]

    def active_tasks(self):
        return self.q("SELECT * FROM tasks WHERE status IN ('scheduled','running') ORDER BY start_ts")

    def duplicate_task(self, uid, wid, nft, label):
        return bool(self.q("SELECT 1 FROM tasks WHERE user_id=? AND wallet_id=? AND nft=? AND stage_label=? "
                           "AND status IN ('scheduled','running')", (uid, wid, nft.lower(), label)))

    def update_task(self, tid, **kw):
        kw["updated"] = int(time.time())
        cols = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE tasks SET {cols} WHERE id=?", (*kw.values(), tid))

    def transition(self, tid, from_status, **kw):
        """Compare-and-set: update the task only if its CURRENT status is in `from_status`.
        Returns True if the row changed. Prevents a stale snapshot (runner tick) from overwriting a newer state
        (e.g. user cancel) and vice versa."""
        kw["updated"] = int(time.time())
        cols = ",".join(f"{k}=?" for k in kw)
        marks = ",".join("?" * len(from_status))
        with self.lock:
            cur = self.c.execute(f"UPDATE tasks SET {cols} WHERE id=? AND status IN ({marks})",
                                 (*kw.values(), tid, *from_status))
            return cur.rowcount == 1
