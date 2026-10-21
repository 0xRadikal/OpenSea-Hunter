#!/usr/bin/env python3
"""Telegram bot: subscription (Telegram Stars) + per-user wallet / OpenSea API key + mint tasks.

Run:  python -m service.bot           (config from env; see service/config.py and SERVICE.md)
"""
import asyncio
import datetime as dt
import html
import json
import logging
import os
import re
import time

from telegram import BotCommand, LabeledPrice, LinkPreviewOptions, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from . import preflight as pf
from . import ui
from .ui import btn, e, kb, title, when
from .config import Config
from .db import DB
from .opensea import OpenSea, OpenSeaError
from .runner import Runner
from .vault import Vault, VaultError, load_master_key, normalize_private_key

log = logging.getLogger("bot")

# Reply-keyboard labels. The user's tap sends exactly this text, so these strings are also the router keys.
# (Custom-emoji icons on reply buttons are attached via icon_custom_emoji_id, not part of the text.)
B_NEW, B_TASKS, B_WAL, B_KEY, B_SET, B_SUB, B_HELP = ("🎯 تسک جدید", "📋 تسک‌ها", "👛 ولت‌ها", "🔑 کلید API",
                                                    "⚙️ تنظیمات", "💳 اشتراک", "❓ راهنما")
B_KEYPAGE = "⚡ ساخت خودکار کلید API"
POLICY_FA = {
    "exact": "فقط با همان قیمت تأییدشده (هر تغییری ← بیخیال)",
    "lower_ok": "اگر قیمت کمتر شد مینت کن، اگر بیشتر شد بیخیال",
    "cap": "تا سقف افزایش مشخص (٪) مینت کن، بیشتر ← بیخیال",
    "any_up": "با هر قیمتی تا سقف خرج ثابت هر عدد (٪) — ریسک بالا",
}
POLICY_SHORT = {"exact": "فقط همان قیمت", "lower_ok": "کمتر شد مینت کن", "cap": "تا سقف ٪ گران‌تر",
                "any_up": "هر تغییری تا سقف ٪"}
STATUS = {  # task status -> (emoji name, Persian label)
    "scheduled": ("timer", "در انتظار"), "running": ("bolt", "در حال اجرا"), "minted": ("ok", "مینت شد"),
    "failed": ("no", "ناموفق"), "cancelled": ("stop", "لغو شده"), "ended": ("wait", "تمام شد"),
    "not_eligible": ("stop", "خارج از allowlist"),
}
EXPIRED = "جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید."
PRIVATE = filters.ChatType.PRIVATE


def fmt_ts(ts):
    """Date/time shown in the reader's own time zone + live relative time (tg-time); UTC text as fallback."""
    return when(ts)


def esc(s):
    return html.escape(str(s))


class Service:
    def __init__(self, cfg: Config, extra_runner_args=None):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, mode=0o700, exist_ok=True)
        self.db = DB(cfg.db_path)
        self.vault = Vault(load_master_key(cfg.master_key_file))
        self.os = OpenSea(cfg.opensea_base)
        self.loop = None
        self.app = None
        self.runner = Runner(self.db, self.vault, cfg, self.notify, extra_runner_args, admin_notify=self.notify_admins,
                             notify_rich=self.notify_rich)

    # ------------------------------------------------------------------ infra
    def notify(self, uid, text, html_=False, markup=None, effect=None):
        """Thread-safe (runner runs in an executor thread). html_=True: text is already HTML-escaped."""
        if self.loop and self.app:
            asyncio.run_coroutine_threadsafe(self._send(uid, text, html_, markup, effect), self.loop)

    def notify_rich(self, uid, html_text, markup=None, effect=None):
        self.notify(uid, html_text, html_=True, markup=markup, effect=effect)

    def notify_admins(self, text):
        for a in self.cfg.admin_ids:
            self.notify(a, text)

    async def _send(self, uid, text, html_=False, markup=None, effect=None):
        try:
            if html_:
                await self.app.bot.send_message(uid, text[:4000], parse_mode=ParseMode.HTML, reply_markup=markup,
                                                message_effect_id=effect)
            else:
                await self.app.bot.send_message(uid, text[:4000], parse_mode=None)
        except Exception as ex:  # noqa: BLE001
            log.warning("notify %s failed: %s", uid, type(ex).__name__)

    def menu(self):
        R = ui.rbtn
        rows = [[R(B_NEW, style="primary"), R(B_TASKS)], [R(B_WAL), R(B_KEY)], [R(B_SET), R(B_SUB)], [R(B_HELP)]]
        if self.cfg.keypage_url:
            rows.insert(2, [R(B_KEYPAGE, web_app=WebAppInfo(self.cfg.keypage_url))])
        return ui.reply_kb(rows, "لینک کالکشن OpenSea یا یکی از گزینه‌ها…")

    async def say(self, u_or_msg, text, markup=None, **kw):
        """Reply in the same chat (HTML)."""
        m = u_or_msg.effective_message if isinstance(u_or_msg, Update) else u_or_msg
        return await m.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, **kw)

    async def show(self, q, text, markup=None):
        """Edit the message the inline button belongs to (one living screen instead of a stream of messages);
        falls back to a new message if it can't be edited (too old / not ours / not modified)."""
        try:
            return await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as ex:  # noqa: BLE001
            if "not modified" in str(ex).lower():
                return q.message
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def alert(self, q, text, show_alert=False):
        await self.safe_answer(q, text[:190], show_alert=show_alert)

    def progress_card(self, uid):
        """Onboarding checklist: subscription -> wallet -> API key -> link."""
        usr = self.db.user(uid)
        sub = bool(usr) and self.db.is_subscribed(uid)
        wal = bool(self.db.wallets(uid))
        key = bool(self.db.apikeys(uid, valid_after=time.time()))
        step = lambda ok, txt: f"{e('ok') if ok else e('timer')} {txt}"
        done = sum((sub, wal, key))
        lines = [step(sub, "اشتراک" + (f" — تا {when(usr['sub_until'], rel=False)}" if sub else "")),
                 step(wal, "ثبت ولت burner"), step(key, "کلید API شخصی OpenSea"),
                 step(False, "ارسال لینک کالکشن و مسلح کردن")]
        return done, f"{ui.bar(done, 3, 9)}  {done}/3\n" + "\n".join(lines)

    async def blocking(self, f, *a):
        return await asyncio.get_running_loop().run_in_executor(None, f, *a)

    @staticmethod
    def unit_bounds(price, policy, pct):
        """(min_unit, max_unit) wei per token the engine may pay under the user's price-change policy."""
        pct = int(pct or 0)
        hi_pct = price + price * pct // 100
        return {"exact": (price, price),
                "lower_ok": (0, price),
                "cap": (price, hi_pct),
                "any_up": (0, hi_pct)}[policy]

    def policy_line(self, pref):
        pol = pref["price_policy"]
        return POLICY_FA[pol] + (f" — سقف +{pref['max_increase_pct']}٪" if pol in ("cap", "any_up") else "")

    def max_active(self):
        return self.db.max_active(self.cfg.max_active_tasks)

    def user_key(self, uid):
        k = self.db.apikey(uid)
        if not k:
            return None
        return self.vault.open(uid, "apikey", k["blob"])

    # ------------------------------------------------------------------ basic
    async def start(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        usr = u.effective_user
        self.db.ensure_user(usr.id, usr.username)
        c.user_data.clear()
        done, card = self.progress_card(usr.id)
        name = esc(usr.first_name or "دوست عزیز")
        txt = (f"{e('robot')} <b>سلام {name}، به Ray Minter خوش آمدید</b>\n"
               "مینت خودکار و فوق‌سریع دراپ‌های OpenSea روی <b>Robinhood Chain</b> — Allowlist و Public، "
               "چند مرحله با تعداد دلخواه، دقیقاً در لحظهٔ شروع.\n\n"
               f"{e('bolt')} شلیک در همان ثانیهٔ شروع مرحله\n"
               f"{e('shield')} کلید ولت رمزنگاری‌شده و بدون هیچ پرداخت اضافه\n"
               f"{e('chart')} گزارش سرعت و رتبه بعد از هر مینت\n\n"
               f"{title('list', 'شروع کار')}\n{card}")
        await self.say(u, txt, self.menu())
        nxt = None
        if not self.db.is_subscribed(usr.id):
            nxt = btn(f"خرید اشتراک {self.cfg.sub_days} روزه — {self.cfg.price_stars} ⭐", "buy", style="success", icon="pstar")
        elif not self.db.wallets(usr.id):
            nxt = btn("افزودن ولت", "wadd", style="success", icon="plus")
        elif not self.db.apikeys(usr.id, valid_after=time.time()):
            nxt = btn("افزودن کلید API", "kset", style="success", icon="plus")
        if nxt:
            await self.say(u, f"{e('next')} <b>قدم بعدی:</b>", kb([nxt]))

    async def help(self, u: Update, c):
        txt = (f"{title('info', 'راهنمای Ray Minter')}\n\n"
               f"<b>{e('next')} چطور کار می‌کند</b>\n"
               "۱. لینک کالکشن را بفرستید ← ۲. ولت را انتخاب کنید ← ۳. مرحله‌ها و تعداد هرکدام را بزنید ← "
               "۴. «مسلح کردن». ربات در لحظهٔ شروع هر مرحله شلیک می‌کند و نتیجه را همین‌جا گزارش می‌دهد.\n\n"
               f"<b>{e('pin')} نکات مهم</b>\n"
               + ui.quote(
                   "• دراپ‌های SeaDrop روی Robinhood Chain: مراحل Allowlist (امضادار) و Public، رایگان یا پولی.\n"
                   "• قبل از ثبت، allowlist و سقف هر مرحله برای همان ولت از OpenSea (ورود SIWE) چک می‌شود.\n"
                   "• سقف هر ولت (Limit N per wallet) <b>جدا برای هر مرحله</b> است؛ مثلاً Limit 4 در مرحلهٔ اول و "
                   "Limit 1 در مرحلهٔ دوم یعنی جمعاً ۵ عدد.\n"
                   "• ربات هرگز بیشتر از سقف، عرضه یا قیمت قابل‌قبول شما ارسال نمی‌کند.", expandable=True) + "\n"
               f"<b>{e('gear')} امکانات</b>\n"
               + ui.quote(
                   "• واکنش به تغییر قیمت در «⚙️ تنظیمات» (فقط همان قیمت / کمتر شد / تا سقف ٪ / هر تغییری تا سقف ٪).\n"
                   "• چند کلید API: هر مرحلهٔ هم‌زمان کلید اختصاصی می‌گیرد تا سهمیهٔ OpenSea تقسیم نشود.\n"
                   "• گزارش سرعت (امضا، ارسال، تأیید) و رتبهٔ شما بین مینت‌کننده‌ها بعد از هر مینت.\n"
                   "• کلیدهای فوری OpenSea ۷ روزه‌اند؛ ربات قبل از انقضا هشدار می‌دهد.", expandable=True) + "\n"
               f"<b>{e('shield')} امنیت</b>\n"
               + ui.quote(
                   "• فقط یک ولت جداگانه (burner) با کمی ETH بسازید و NFT را بعد از مینت منتقل کنید.\n"
                   "• کلید خصوصی بلافاصله از چت پاک و با AES-256-GCM رمزنگاری می‌شود.\n"
                   "• هرگز کلید ولت اصلی خود را وارد نکنید.") + "\n"
               "<b>دستورات:</b> /start · /cancel · /wallets · /key · /tasks · /sub · /settings")
        await self.say(u, txt)

    async def cancel_cmd(self, u: Update, c):
        c.user_data.clear()
        await self.say(u, f"{e('stop')} لغو شد. هر وقت خواستید از منو ادامه دهید.", self.menu())

    # ------------------------------------------------------------------ subscription
    async def sub(self, u: Update, c):
        usr = self.db.ensure_user(u.effective_user.id, u.effective_user.username)
        if self.db.is_subscribed(usr["id"]):
            left = max(0, usr["sub_until"] - time.time()) / 86400
            s = (f"{title('gem', 'اشتراک شما')}\n\n{e('green')} <b>فعال</b> — {left:.1f} روز باقی‌مانده\n"
                 f"{ui.kv('پایان', fmt_ts(usr['sub_until']))}\n\n"
                 f"تمدید، {self.cfg.sub_days} روز به انتهای اشتراک فعلی اضافه می‌کند.")
            label = f"تمدید {self.cfg.sub_days} روزه — {self.cfg.price_stars} ⭐"
        else:
            s = (f"{title('gem', 'اشتراک Ray Minter')}\n\n{e('red')} اشتراک فعال ندارید.\n\n"
                 f"{e('ok')} مینت خودکار Allowlist و Public، چند مرحله و چند ولت\n"
                 f"{e('ok')} گزارش سرعت و رتبه بعد از هر مینت\n"
                 f"{e('ok')} تا {self.max_active()} تسک فعال هم‌زمان\n\n"
                 f"{ui.kv('مدت', f'{self.cfg.sub_days} روز')} · {ui.kv('قیمت', f'{self.cfg.price_stars} ⭐ Telegram Stars')}")
            label = f"خرید اشتراک {self.cfg.sub_days} روزه — {self.cfg.price_stars} ⭐"
        await self.say(u, s, kb([btn(label, "buy", style="success", icon="pstar")]))

    async def buy(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        await c.bot.send_invoice(uid, title=f"اشتراک {self.cfg.sub_days} روزه Ray Minter",
                                 description=f"مینت خودکار دراپ‌های OpenSea روی Robinhood Chain — {self.cfg.sub_days} روز",
                                 payload=f"sub:{self.cfg.sub_days}:{uid}", currency="XTR", provider_token="",
                                 prices=[LabeledPrice("اشتراک", self.cfg.price_stars)])

    async def precheckout(self, u: Update, c):
        q = u.pre_checkout_query
        ok = (q.currency == "XTR" and q.total_amount == self.cfg.price_stars
              and q.invoice_payload == f"sub:{self.cfg.sub_days}:{q.from_user.id}")
        await q.answer(ok=ok, error_message=None if ok else "فاکتور منقضی است؛ دوباره از منوی اشتراک اقدام کنید.")

    async def paid(self, u: Update, c):
        p = u.message.successful_payment
        uid = u.effective_user.id
        try:
            days = int(p.invoice_payload.split(":")[1])
        except Exception:  # noqa: BLE001
            days = self.cfg.sub_days
        new = self.db.record_payment(p.telegram_payment_charge_id, uid, p.total_amount, p.invoice_payload, days)
        self.db.audit(uid, "payment", f"{p.total_amount} XTR {p.telegram_payment_charge_id}")
        if new:
            await u.message.reply_text(
                f"{e('party')} <b>پرداخت موفق — خوش آمدید!</b>\n\n{e('gem')} اشتراک فعال تا {fmt_ts(new)}\n\n"
                f"{e('next')} قدم بعدی: ولت burner و کلید API را ثبت کنید، بعد لینک کالکشن را بفرستید.",
                parse_mode=ParseMode.HTML, reply_markup=self.menu(), message_effect_id=ui.EFFECT_PARTY)

    def need_sub(self, uid):
        return None if self.db.is_subscribed(uid) else f"{e('lock')} برای این بخش اشتراک فعال لازم است (💳 اشتراک)."

    # ------------------------------------------------------------------ wallets
    def wallets_view(self, uid):
        ws = self.db.wallets(uid)
        lines = [f"{title('coin', 'ولت‌های شما')}  <i>({len(ws)} از ۵)</i>\n"]
        for n, w in enumerate(ws, 1):
            busy = self.db.q("SELECT COUNT(*) FROM tasks WHERE wallet_id=? AND status IN ('scheduled','running')", (w["id"],))[0][0]
            lines.append(f"<b>{n}.</b> {ui.code(w['address'])}"
                         + (f"\n     {e('bolt')} {busy} تسک فعال" if busy else ""))
        if not ws:
            lines.append(f"{e('info')} هنوز ولتی ثبت نکرده‌اید.\n")
        lines.append("\n" + ui.quote(f"{e('shield')} فقط ولت جداگانه (burner) با کمی ETH برای گس — هرگز ولت اصلی."))
        rows = [[btn(f"{ui.short(w['address'])}", copy=w["address"], icon="link"),
                 btn("حذف", f"wdq:{w['id']}", style="danger", icon="trash")] for w in ws]
        if len(ws) < 5:
            rows.append([btn("افزودن ولت", "wadd", style="success", icon="plus")])
        return "\n".join(lines), kb(*rows)

    async def wallets(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        txt, mk = self.wallets_view(uid)
        await self.say(u, txt, mk)

    async def wallet_del_ask(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        w = self.db.wallet(q.from_user.id, int(q.data.split(":")[1]))
        if not w:
            return await self.show(q, *self.wallets_view(q.from_user.id))
        await self.show(q, f"{e('warn')} <b>حذف این ولت؟</b>\n{ui.code(w['address'])}\n\n"
                           "کلید رمزنگاری‌شدهٔ آن از ربات پاک می‌شود (دارایی ولت دست نمی‌خورد).",
                        kb([btn("بله، حذف کن", f"wdel:{w['id']}", style="danger", icon="trash"),
                            btn("نه، برگرد", "wls", icon="next")]))

    async def wallets_back(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        await self.show(q, *self.wallets_view(q.from_user.id))

    async def wallet_add(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        if (m := self.need_sub(q.from_user.id)):
            return await self.say(q.message, m, kb([btn("خرید اشتراک", "buy", style="success", icon="pstar")]))
        if len(self.db.wallets(q.from_user.id)) >= 5:
            return await self.alert(q, "حداکثر ۵ ولت مجاز است.", True)
        c.user_data["state"] = "await_pk"
        await self.say(q.message,
                       f"{title('lock', 'افزودن ولت burner')}\n\n"
                       "کلید خصوصی (Private Key) ولت را بفرستید — ۶۴ کاراکتر hex (با یا بدون 0x).\n\n"
                       f"{e('warn')} <b>فقط ولت جداگانه با کمی ETH برای گس — نه ولت اصلی!</b>\n"
                       f"{e('shield')} پیام شما همان لحظه پاک و کلید با AES-256-GCM رمزنگاری می‌شود.",
                       kb([btn("انصراف", "xc", style="danger", icon="no")]))

    async def wallet_del(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        wid = int(q.data.split(":")[1])
        ok = self.db.delete_wallet(q.from_user.id, wid)
        self.db.audit(q.from_user.id, "wallet_delete", f"{wid} {ok}")
        if not ok:
            await self.alert(q, "این ولت تسک فعال دارد؛ اول تسک را لغو کنید.", True)
        txt, mk = self.wallets_view(q.from_user.id)
        await self.show(q, (f"{e('ok')} ولت حذف شد.\n\n" if ok else "") + txt, mk)

    async def got_pk(self, u: Update, c):
        uid = u.effective_user.id
        text = u.message.text or ""
        try:
            await u.message.delete()
        except Exception:  # noqa: BLE001
            await u.effective_chat.send_message(f"{e('warn')} نتوانستم پیام را پاک کنم؛ <b>لطفاً همین الان خودتان آن را پاک کنید.</b>",
                                                parse_mode=ParseMode.HTML)
        send = lambda t, mk=None: u.effective_chat.send_message(t, parse_mode=ParseMode.HTML, reply_markup=mk)
        try:
            pk = normalize_private_key(text)
        except Exception:  # noqa: BLE001
            return await send(f"{e('no')} کلید معتبر نیست (۶۴ کاراکتر hex). دوباره بفرستید یا انصراف بزنید.",
                              kb([btn("انصراف", "xc", style="danger", icon="no")]))
        addr, blob = self.vault.encrypt(uid, pk)
        try:
            bal = await self.blocking(pf.check_wallet, addr)
        except pf.PreflightError as ex:
            return await send(f"{e('no')} {esc(ex)}")
        self.db.add_wallet(uid, addr, blob)
        self.db.audit(uid, "wallet_add", addr)
        c.user_data.pop("state", None)
        warn = "" if bal >= pf.WARN_BALANCE_WEI else \
            f"\n\n{e('warn')} موجودی برای گس کم است؛ حداقل <b>0.0001 ETH</b> روی Robinhood Chain واریز کنید."
        await send(f"{e('ok')} <b>ولت ثبت شد</b>\n{ui.code(addr)}\n{ui.kv('موجودی', ui.eth(bal))}{warn}",
                   kb([btn("کپی آدرس", copy=addr, icon="link"), btn("اکسپلورر", url=ui.addr_url(addr), icon="search")]))
        await send(f"{e('lock')} کلید خصوصی پاک و رمزنگاری شد.", self.menu())

    # ------------------------------------------------------------------ OpenSea API key
    def key_menu(self, uid):
        ks = self.db.apikeys(uid)
        now = time.time()
        valid = sum(1 for k in ks if not (k["expires_at"] and k["expires_at"] < now))
        lines = [f"{title('lock', 'کلیدهای API شخصی OpenSea')}  <i>({valid} معتبر از {len(ks)})</i>\n"]
        if not ks:
            lines.append(f"{e('red')} هنوز کلیدی ثبت نشده — برای مینت حداقل یک کلید لازم است.\n")
        src = {"instant": "فوری", "manual": "دستی", "legacy": "قدیمی"}
        for n, k in enumerate(ks, 1):
            busy = self.db.q("SELECT COUNT(*) FROM tasks WHERE apikey_id=? AND status IN ('scheduled','running')", (k["id"],))[0][0]
            dead = k["expires_at"] and k["expires_at"] < now
            soon = not dead and k["expires_at"] and k["expires_at"] - now < 86400
            st = e("red") if dead else (e("warn") if soon else e("green"))
            lines.append(f"{st} <b>کلید {esc(k['label'] or k['id'])}</b> · {src.get(k['source'], esc(k['source']))}"
                         + (f" · {e('bolt')} {busy} تسک" if busy else "") + "\n     "
                         + ("منقضی شده" if dead else (f"انقضا {fmt_ts(k['expires_at'])}" if k["expires_at"] else "انقضا: نامشخص")))
        lines.append("\n" + ui.quote(
            f"{e('idea')} هر مرحله کلید اختصاصی خودش را می‌گیرد تا سهمیهٔ ۵ درخواست مینت OpenSea بین مرحله‌های "
            "هم‌زمان تقسیم نشود.\n"
            f"{e('warn')} کلیدهای یک حساب OpenSea سهمیهٔ مشترک دارند و کلید فوری روزانه ۲ عدد برای هر IP است؛ "
            "کلیدها را از اینترنت/حساب‌های مختلف بسازید.\n\n"
            "<b>روش ۱:</b> دکمهٔ «⚡ ساخت خودکار کلید API» در منو (از اینترنت خود شما).\n"
            "<b>روش ۲:</b> <code>curl -X POST https://api.opensea.io/api/v2/auth/keys</code> یا کلید کامل از "
            "opensea.io/settings/developer و ارسال همین‌جا.", expandable=True))
        rows = [[btn(f"حذف کلید {k['label'] or k['id']}", f"kdq:{k['id']}", style="danger", icon="trash")] for k in ks]
        rows.append([btn("افزودن کلید (دستی)", "kset", style="success", icon="plus")])
        return "\n".join(lines), kb(*rows)

    async def key(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        txt, mk = self.key_menu(uid)
        await self.say(u, txt, mk)

    async def key_del_ask(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        kid = int(q.data.split(":")[1])
        k = self.db.apikey_by_id(q.from_user.id, kid)
        if not k:
            return await self.show(q, *self.key_menu(q.from_user.id))
        await self.show(q, f"{e('warn')} <b>حذف کلید {esc(k['label'] or k['id'])}؟</b>\n"
                           "تسک‌هایی که بعداً شروع می‌شوند از کلیدهای دیگر شما استفاده می‌کنند.",
                        kb([btn("بله، حذف کن", f"kdel:{kid}", style="danger", icon="trash"),
                            btn("نه، برگرد", "kls", icon="next")]))

    async def keys_back(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        await self.show(q, *self.key_menu(q.from_user.id))

    async def key_del(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        kid = int(q.data.split(":")[1])
        ok = self.db.delete_apikey(q.from_user.id, kid)
        self.db.audit(q.from_user.id, "apikey_delete", f"{kid} {ok}")
        if not ok:
            await self.alert(q, "این کلید به تسک فعال اختصاص دارد؛ اول تسک را لغو کنید.", True)
        txt, mk = self.key_menu(q.from_user.id)
        await self.show(q, (f"{e('ok')} کلید حذف شد.\n\n" if ok else "") + txt, mk)

    async def key_set(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        c.user_data["state"] = "await_key"
        await self.say(q.message, f"{title('lock', 'افزودن کلید API')}\n\nکلید API یا <b>کل خروجی JSON</b> دستور curl را بفرستید.\n"
                                  f"{e('shield')} پیام شما بعد از دریافت پاک می‌شود.",
                       kb([btn("انصراف", "xc", style="danger", icon="no")]))

    async def store_key(self, u: Update, c, key, expires_at, source):
        uid = u.effective_user.id
        try:
            ok, why = await self.blocking(self.os.test_key, key)
        except OpenSeaError as ex:
            return await u.effective_chat.send_message(f"{e('warn')} تست کلید ممکن نشد: {esc(ex)}. بعداً دوباره امتحان کنید.",
                                                       parse_mode=ParseMode.HTML)
        if not ok:
            return await u.effective_chat.send_message(f"{e('no')} کلید توسط OpenSea رد شد: {esc(why)}", parse_mode=ParseMode.HTML)
        import hashlib
        fp = hashlib.sha256(("osh-key|" + key).encode()).hexdigest()[:32]
        r = self.db.add_apikey(uid, self.vault.seal(uid, "apikey", key), expires_at, source, fp)
        c.user_data.pop("state", None)
        H = ParseMode.HTML
        if r == "dup":
            return await u.effective_chat.send_message(f"{e('info')} این کلید قبلاً ثبت شده است.", parse_mode=H, reply_markup=self.menu())
        if r == "full":
            return await u.effective_chat.send_message(f"{e('no')} حداکثر {self.db.MAX_KEYS} کلید مجاز است؛ یکی را حذف کنید.", parse_mode=H)
        self.db.audit(uid, "apikey_add", f"{r} {source}")
        exp = f"\n{ui.kv('انقضا', fmt_ts(expires_at))}" if expires_at else ""
        n = len(self.db.apikeys(uid, valid_after=time.time()))
        await u.effective_chat.send_message(f"{e('ok')} <b>کلید API تست و ذخیره شد.</b> (کلیدهای معتبر شما: {n})" + exp,
                                            parse_mode=H, reply_markup=self.menu())

    @staticmethod
    def parse_key_text(text):
        t = (text or "").strip()
        exp = None
        if t.startswith("{"):
            try:
                j = json.loads(t)
            except ValueError:
                return None, None
            if j.get("errors"):
                return "ERR:" + str(j["errors"][0])[:150], None
            t = str(j.get("api_key") or "")
            if j.get("expires_at"):
                try:
                    exp = int(dt.datetime.fromisoformat(j["expires_at"].replace("Z", "+00:00")).timestamp())
                except ValueError:
                    exp = None
        return t, exp

    async def got_key(self, u: Update, c):
        key, exp = self.parse_key_text(u.message.text)
        try:
            await u.message.delete()
        except Exception:  # noqa: BLE001
            pass
        if key and key.startswith("ERR:"):
            return await u.effective_chat.send_message(f"{e('no')} OpenSea خطا داد: {esc(key[4:])}\n<i>سقف ساخت کلید فوری ۲ عدد در روز برای هر IP است.</i>", parse_mode=ParseMode.HTML)
        if not key:
            return await u.effective_chat.send_message(f"{e('no')} کلید پیدا نشد. دوباره بفرستید یا /cancel", parse_mode=ParseMode.HTML)
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "manual")

    async def webapp_data(self, u: Update, c):
        data = u.effective_message.web_app_data.data
        key, exp = self.parse_key_text(data)
        if key and key.startswith("ERR:"):
            return await u.effective_chat.send_message(f"{e('no')} OpenSea خطا داد: {esc(key[4:])}\n<i>سقف ساخت کلید فوری ۲ عدد در روز برای هر IP است.</i>", parse_mode=ParseMode.HTML)
        if not key:
            return await u.effective_chat.send_message(f"{e('no')} داده نامعتبر از صفحهٔ ساخت کلید", parse_mode=ParseMode.HTML)
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "instant")

    # ------------------------------------------------------------------ tasks
    async def new_task(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        if (m := self.need_sub(uid)):
            return await self.say(u, m, kb([btn(f"خرید اشتراک — {self.cfg.price_stars} ⭐", "buy", style="success", icon="pstar")]))
        if not self.db.apikey(uid):
            return await self.say(u, f"{e('lock')} اول کلید API شخصی OpenSea را ثبت کنید.",
                                  kb([btn("افزودن کلید API", "kset", style="success", icon="plus")]))
        if not self.db.wallets(uid):
            return await self.say(u, f"{e('coin')} اول یک ولت burner ثبت کنید.",
                                  kb([btn("افزودن ولت", "wadd", style="success", icon="plus")]))
        if self.db.active_count(uid) >= self.max_active():
            return await self.say(u, f"{e('stop')} حداکثر {self.max_active()} تسک فعال مجاز است؛ صبر کنید یکی تمام شود یا لغوش کنید.")
        c.user_data["state"] = "await_link"
        await self.say(u, f"{title('link', 'تسک جدید')}\n\nلینک کالکشن OpenSea (یا فقط slug) را بفرستید.\n"
                          f"<i>مثال:</i> <code>https://opensea.io/collection/aerent</code>",
                       kb([btn("انصراف", "xc", style="danger", icon="no")]))

    @staticmethod
    def extract_link(text):
        """The OpenSea link/slug inside the user's message, or None. Accepts a bare slug, a bare link, or a
        longer pasted/forwarded text that contains exactly one opensea.io link."""
        t = (text or "").strip()
        if not t:
            return None
        if not any(ch.isspace() for ch in t):
            return t
        links = set(re.findall(r"(?:https?://)?(?:www\.)?opensea\.io/[^\s<>\"']+", t, flags=re.I))
        return links.pop() if len(links) == 1 else None

    async def got_link(self, u: Update, c):
        uid = u.effective_user.id
        link = self.extract_link(u.message.text)
        if not link:
            return await self.say(u, f"{e('warn')} این پیام لینک کالکشن نیست. فقط لینک را بفرستید، مثل:\n"
                                     "<code>https://opensea.io/collection/aerent</code>\n<i>(یا فقط slug مثل aerent)</i>",
                                  kb([btn("انصراف", "xc", style="danger", icon="no")]))
        await u.effective_chat.send_action("typing")
        msg = await self.say(u, f"{e('search')} در حال بررسی دراپ روی OpenSea و زنجیره…")
        try:
            key = self.user_key(uid)
            slug = await self.blocking(self.os.resolve_slug, link, key)
            drop = await self.blocking(self.os.drop, slug, key)
            nft = pf.check_drop(drop)
            await self.blocking(pf.check_chain_contract, nft)
        except (OpenSeaError, pf.PreflightError, VaultError) as ex:
            return await msg.edit_text(f"{e('no')} {esc(ex)}", parse_mode=ParseMode.HTML,
                                       reply_markup=kb([btn("انصراف", "xc", style="danger", icon="no")]))
        stages = pf.pick_stages(drop)
        c.user_data.pop("state", None)
        if not stages:
            return await msg.edit_text(f"{e('wait')} همهٔ مرحله‌های این دراپ تمام شده‌اند.", parse_mode=ParseMode.HTML)
        ok_st, lines = [], []
        for s in stages:
            why = pf.unsupported_reason(s)
            lim = pf.stage_wallet_limit(s)
            lines.append(f"{e('green') if not why else e('stop')} <b>{esc(s['label'])}</b> · {self.kind_fa(s['kind'], s['type'])}\n"
                         f"     {self.price_fa(s['price_wei'])} · سقف هر ولت: {lim if lim else '?'}\n"
                         f"     {fmt_ts(s['start'])}"
                         + (f"\n     ↳ <i>{esc(why)}</i>" if why else ""))
            if not why:
                ok_st.append(s)
        try:
            tot, mx = int(drop.get("total_supply") or 0), int(drop.get("max_supply") or 0)
        except (TypeError, ValueError):
            tot = mx = 0
        sup = f"{drop.get('total_supply')}/{drop.get('max_supply')}" + (f"  {ui.bar(tot, mx)}" if mx else "")
        txt = (f"{e('gem')} <b>{esc(drop.get('collection_name') or slug)}</b>\n"
               f"{ui.kv('قرارداد', ui.code(nft))}\n{ui.kv('عرضه', sup)}\n{ui.SEP}\n" + "\n\n".join(lines))
        if not ok_st:
            return await msg.edit_text(txt + f"\n\n{e('no')} هیچ مرحلهٔ قابل‌مینتی ندارد.", parse_mode=ParseMode.HTML)
        c.user_data["plan"] = {"step": "wallet", "slug": slug, "nft": nft, "name": drop.get("collection_name") or slug,
                               "stages": ok_st}
        await msg.edit_text(txt + f"\n{ui.SEP}\n{e('coin')} <b>با کدام ولت مینت شود؟</b>\n"
                                  "<i>ربات برای همان ولت allowlist و سقف هر مرحله را از OpenSea چک می‌کند.</i>",
                            parse_mode=ParseMode.HTML, reply_markup=self.wallet_pick_kb(uid),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))

    def wallet_pick_kb(self, uid):
        rows = [[btn(f"{ui.short(w['address'])}", f"pw:{w['id']}", icon="coin")] for w in self.db.wallets(uid)]
        rows.append([btn("انصراف", "px", style="danger", icon="no")])
        return kb(*rows)

    @staticmethod
    def kind_fa(kind, typ=None):
        return {"signed": "Allowlist (امضادار)", "public": "Public (عمومی)"}.get(kind, esc(typ or "?"))

    @staticmethod
    def price_fa(p):
        return "رایگان" if p == 0 else (f"{p / 1e18:.6f} ETH" if p else "?")

    async def plan_wallet(self, u: Update, c):
        """Per-stage pre-check for the chosen wallet: OpenSea eligibility (SIWE) incl. the per-stage wallet cap,
        already minted, funds. Per-wallet limits are PER STAGE (verified live 2026-10-21, nazism-doc): OpenSea's
        /eligibility returns max_total_mintable_by_wallet = the absolute total this wallet may reach in that stage
        (minted elsewhere + that stage's limit), and SeaDrop checks numMinted + qty <= it."""
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        p = c.user_data.get("plan")
        w = self.db.wallet(uid, int(q.data.split(":")[1]))
        if not (p and w):
            return await self.say(q.message, f"{e('wait')} {EXPIRED}")
        nft, slug = p["nft"], p["slug"]
        if p.get("step") != "wallet":                                # double tap on a wallet button
            return
        p["step"] = "checking"
        m = await self.show(q, f"{e('search')} <b>پیش‌بررسی ولت</b> {ui.code(ui.short(w['address']))}\n\n"
                               f"{e('wait')} موجودی و مینت‌های قبلی روی زنجیره\n"
                               f"{e('wait')} ورود به OpenSea با ولت (SIWE)\n"
                               f"{e('wait')} allowlist و سقف هر مرحله\n\n<i>چند ثانیه صبر کنید…</i>")
        try:
            bal = await self.blocking(pf.check_wallet, w["address"])
            done = await self.blocking(pf.minted_count, nft, w["address"])
        except pf.PreflightError as ex:
            p["step"] = "wallet"                                     # let the user retry / pick another wallet
            return await m.edit_text(f"{e('no')} {esc(ex)}\n\n{e('coin')} <b>با کدام ولت مینت شود؟</b>",
                                     parse_mode=ParseMode.HTML, reply_markup=self.wallet_pick_kb(uid))
        pub = None
        if any(s["kind"] == "public" for s in p["stages"]):
            try:
                pub = await self.blocking(pf.onchain_public_drop, nft)
            except pf.PreflightError:
                pub = None
        el, el_err = {}, None
        try:
            pk = self.vault.decrypt(uid, w["address"], w["blob"])
            jwt = await self.blocking(self.os.wallet_jwt, pk)
            del pk
            el = await self.blocking(self.os.eligibility_full, slug, self.user_key(uid), jwt)
        except (OpenSeaError, VaultError) as ex:
            el_err = str(ex)[:80]
        for s in p["stages"]:
            s["limit"] = pf.stage_wallet_limit(s)                    # per-stage limit as shown by OpenSea
            s["note"] = None
            if s["kind"] == "public" and pub and s["price_wei"] > 0 and pub[0] != s["price_wei"]:
                s["note"] = f"قیمت روی زنجیره ({pub[0] / 1e18:.6f}) با OpenSea فرق دارد"
            el_s = el.get(s["uuid"]) or {}
            s["eligible"] = el_s.get("eligible") if el_s else (True if s["kind"] == "public" else None)
            s["cap"] = el_s.get("cap")                                  # absolute cap for THIS wallet in THIS stage
            if s["cap"] is not None:
                room = s["cap"] - done
            elif s["limit"]:
                room = s["limit"]                                    # unknown cap: assume the stage limit (per stage)
            else:
                room = pf.API_MAX_QTY
            s["room"] = room
            fund = ((bal - pf.WARN_BALANCE_WEI // 4) // s["price_wei"]) if s["price_wei"] > 0 else pf.API_MAX_QTY
            s["maxq"] = max(0, min(pf.API_MAX_QTY, room, fund))
            s["why_no"] = ("طبق OpenSea این ولت در allowlist این مرحله نیست" if s["eligible"] is False else
                           s["note"] if s["note"] else
                           f"سقف این مرحله برای این ولت پر است (Limit {s['limit']} per wallet)" if room <= 0 else
                           "موجودی برای حتی ۱ عدد کافی نیست" if s["maxq"] < 1 else None)
            s["qty"] = None
        p.update(step="select", wid=w["id"], addr=w["address"], bal=bal, done=done, el_err=el_err)
        await self.render_select(m, p)

    def select_text(self, p):
        lines = [f"{e('coin')} {ui.code(p['addr'])}\n"
                 f"{ui.kv('موجودی', ui.eth(p['bal']))} · {ui.kv('مینت‌های قبلی در این کالکشن', p['done'])}\n{ui.SEP}"]
        for s in p["stages"]:
            elig = {True: f"{e('ok')} allowlist هست", False: f"{e('no')} allowlist نیست"}.get(s["eligible"], f"{e('warn')} allowlist نامعلوم")
            if s["kind"] == "public":
                elig = f"{e('green')} عمومی (allowlist ندارد)" if s["eligible"] is not False else elig
            mark = e("ok") if s["qty"] else (e("stop") if s["why_no"] else e("timer"))
            lines.append(f"{mark} <b>{esc(s['label'])}</b> · {self.kind_fa(s['kind'])} · {self.price_fa(s['price_wei'])}\n"
                         f"     {fmt_ts(s['start'])}\n"
                         f"     {elig} · Limit {s['limit'] if s['limit'] else '?'} per wallet\n     "
                         + (f"↳ <i>{esc(s['why_no'])}</i>" if s["why_no"] else f"قابل مینت برای شما: حداکثر <b>{s['maxq']}</b>")
                         + (f"\n     {e('pin')} انتخاب شما: <b>{s['qty']} عدد</b>" if s["qty"] else ""))
        if p.get("el_err"):
            lines.append(f"{e('warn')} چک allowlist ممکن نشد ({esc(p['el_err'])})؛ در زمان مینت دوباره چک می‌شود.")
        lines.append(ui.quote(f"{e('info')} سقف هر ولت <b>جدا برای هر مرحله</b> است (طبق OpenSea): مثلاً Limit 4 در "
                              "مرحلهٔ اول و Limit 1 در مرحلهٔ دوم یعنی جمعاً ۵ عدد."))
        lines.append(f"{e('next')} مرحله(ها) را انتخاب و تعداد هرکدام را مشخص کنید، بعد «ادامه».")
        return "\n".join(lines)

    async def render_select(self, msg, p, new=False):
        rows = []
        for i, s in enumerate(p["stages"]):
            if s["why_no"]:
                continue
            if s["qty"]:
                rows.append([btn(f"{s['label'][:30]} × {s['qty']}", f"ps:{i}", style="success", icon="ok")])
            else:
                rows.append([btn("⬜ " + s["label"][:30], f"ps:{i}")])
        if any(s["qty"] for s in p["stages"]):
            rows.append([btn("ادامه — خلاصه و تأیید", "pn", style="primary", icon="next")])
        rows.append([btn("انصراف", "px", style="danger", icon="no")])
        f = msg.reply_text if new else msg.edit_text
        await f(self.select_text(p), parse_mode=ParseMode.HTML, reply_markup=kb(*rows))

    @staticmethod
    def qty_options(maxq):
        return [n for n in (1, 2, 3, 5, 10, 20, 50) if n < maxq]

    async def safe_answer(self, q, *a, **k):
        """answerCallbackQuery is cosmetic (removes the spinner). A Telegram timeout there (seen live 2026-10-21:
        httpx.ReadTimeout in answer_callback_query) must NOT abort the handler."""
        try:
            await q.answer(*a, **k)
        except Exception as ex:  # noqa: BLE001
            log.warning("answerCallbackQuery failed (ignored): %s", type(ex).__name__)

    async def plan_stage(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        p = c.user_data.get("plan")
        try:
            i = int(q.data.split(":")[1]); s = p["stages"][i]
            assert p["step"] in ("select", "qty") and not s["why_no"]
        except (TypeError, KeyError, IndexError, ValueError, AssertionError):
            return await self.say(q.message, f"{e('wait')} {EXPIRED}")
        if s["qty"]:                                             # tap on a selected stage = deselect
            s["qty"] = None
            return await self.render_select(q.message, p)
        p["step"] = "qty"; p["qi"] = i
        lim = s["limit"]
        opts = self.qty_options(s["maxq"])
        rows = [[btn(str(n), f"pq:{i}:{n}") for n in opts[k:k + 4]] for k in range(0, len(opts), 4)]
        rows.append([btn(f"حداکثر ({s['maxq']})", f"pq:{i}:max", style="primary", icon="top")])
        if s["maxq"] > 1:
            rows.append([btn("عدد دلخواه", f"pq:{i}:c", icon="edit")])
        rows.append([btn("برگشت", "pb", icon="next")])
        cost = "" if not s["price_wei"] else f"\n{e('usd')} قیمت هر عدد: {s['price_wei'] / 1e18:.6f} ETH"
        await q.message.edit_text(
            f"{e('pin')} <b>{esc(s['label'])}</b>\n\n"
            f"Limit {lim if lim else '?'} per wallet — سقف هر ولت این مرحله {lim if lim else 'نامشخص'} عدد است.\n"
            f"حداکثر قابل مینت برای شما در این مرحله: <b>{s['maxq']}</b>{cost}\n\n"
            f"{e('next')} <b>می‌خواهید در این مرحله چند عدد مینت کنید؟</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb(*rows))

    async def plan_qty(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        p = c.user_data.get("plan")
        try:
            _, i, v = q.data.split(":"); i = int(i); s = p["stages"][i]
            assert p["step"] == "qty" and p.get("qi") == i
        except (TypeError, KeyError, IndexError, ValueError, AssertionError):
            return await self.say(q.message, f"{e('wait')} {EXPIRED}")
        if v == "c":
            c.user_data["state"] = "await_qty"
            p["qmsg"] = q.message.message_id
            return await self.say(q.message, f"{e('edit')} عدد مورد نظر را بفرستید (۱ تا {s['maxq']}).",
                                  kb([btn("برگشت", "pb", icon="next")]))
        n = s["maxq"] if v == "max" else int(v)
        if not 1 <= n <= s["maxq"]:
            return await self.alert(q, f"عدد باید بین ۱ و {s['maxq']} باشد.", True)
        s["qty"] = n; p["step"] = "select"; p.pop("qi", None)
        await self.render_select(q.message, p)

    async def got_qty(self, u: Update, c):
        p = c.user_data.get("plan")
        t = (u.message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if not (p and p.get("step") == "qty" and p.get("qi") is not None):
            c.user_data.pop("state", None)
            return await self.say(u, f"{e('wait')} {EXPIRED}")
        s = p["stages"][p["qi"]]
        if not t.isdigit() or not 1 <= int(t) <= s["maxq"]:
            return await self.say(u, f"{e('no')} یک عدد بین ۱ و {s['maxq']} بفرستید (یا /cancel).")
        s["qty"] = int(t); p["step"] = "select"; p.pop("qi", None)
        c.user_data.pop("state", None)
        await self.render_select(u.message, p, new=True)

    async def plan_back(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        p = c.user_data.get("plan")
        if not p or "wid" not in p:
            return await self.say(q.message, f"{e('wait')} {EXPIRED}")
        p["step"] = "select"; p.pop("qi", None); c.user_data.pop("state", None)
        await self.render_select(q.message, p)

    @staticmethod
    def project(p):
        """Selected stages in start order. Limits are per stage (OpenSea signs a per-stage absolute cap that already
        accounts for mints in other stages), so one stage's mints do not reduce another stage's allowance."""
        return [(s, s["qty"]) for s in sorted((s for s in p["stages"] if s["qty"]), key=lambda s: s["start"])]

    async def plan_next(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        p = c.user_data.get("plan")
        if not p or p.get("step") != "select" or not any(s["qty"] for s in p["stages"]):
            return await self.say(q.message, f"{e('wait')} {EXPIRED}")
        proj = self.project(p)
        pref = self.db.prefs(uid)
        for s, _ in proj:
            s["min_unit"], s["max_unit"] = self.unit_bounds(s["price_wei"], pref["price_policy"], pref["max_increase_pct"])
        total = sum(s["max_unit"] * s["qty"] for s, _ in proj)
        gas = pf.WARN_BALANCE_WEI * len(proj)
        lines, ok = [f"{title('list', 'خلاصهٔ برنامهٔ مینت')}\n{e('gem')} {esc(p['name'])}\n"
                     f"{e('coin')} {ui.code(p['addr'])}\n{ui.SEP}"], True
        for n, (s, eff) in enumerate(proj, 1):
            pr, qt = s["price_wei"], s["qty"]
            cost = "رایگان" if not pr else f"{pr / 1e18:.6f} × {qt} = {pr * qt / 1e18:.6f} ETH"
            lines.append(f"<b>{n}. {esc(s['label'])}</b> · {self.kind_fa(s['kind'])}\n"
                         f"     {fmt_ts(s['start'])}\n"
                         f"     تعداد: <b>{qt}</b> · {cost}")
            if s["max_unit"] != pr or s["min_unit"] != pr:
                lines.append(f"     قیمت قابل‌قبول هر عدد: {s['min_unit'] / 1e18:.6f} تا {s['max_unit'] / 1e18:.6f} ETH")
        lines.append(f"{ui.SEP}\n{e('usd')} <b>جمع قیمت (حداکثر):</b> {total / 1e18:.6f} ETH + گس (~{gas / 1e18:.4f})\n"
                     f"{e('coin')} <b>موجودی:</b> {p['bal'] / 1e18:.6f} ETH")
        if p["bal"] < total + gas:
            lines.append(f"{e('warn')} موجودی برای همهٔ برنامه کافی نیست؛ ربات در هر مرحله فقط به اندازهٔ موجودی همان لحظه مینت می‌کند.")
        last = max(s["start"] for s, _ in proj)
        lines.append(f"\n{e('gear')} <b>اگر قیمت عوض شد:</b> {self.policy_line(pref)}  <i>(تغییر در «⚙️ تنظیمات»)</i>")
        assign = self.db.pick_keys(uid, [{"start": s["start"], "end": s["end"]} for s, _ in proj])
        if any(kid is None for kid, _ in assign):
            ok = False
            lines.append(f"{e('no')} برای بعضی مرحله‌ها هیچ کلید API معتبری تا زمان شروع ندارید؛ کلید جدید ثبت کنید.")
        else:
            labels = {k["id"]: k["label"] or str(k["id"]) for k in self.db.apikeys(uid)}
            shared = sum(1 for _, sh in assign if sh)
            lines.append(f"{e('lock')} <b>کلید هر مرحله:</b> " + "، ".join(f"{esc(s['label'][:16])} ← {labels.get(kid, kid)}"
                                                                         for (s, _), (kid, _) in zip(proj, assign)))
            if shared:
                lines.append(f"{e('warn')} {shared} مرحله با مرحلهٔ هم‌زمان دیگری کلید مشترک دارد (سهمیهٔ ۵ درخواست مینت OpenSea "
                             "تقسیم می‌شود). برای کلید اختصاصی کلید بیشتری اضافه کنید.")
        usr = self.db.user(uid)
        if usr and usr["sub_until"] < last:
            ok = False
            lines.append(f"{e('no')} اشتراک شما قبل از شروع آخرین مرحله تمام می‌شود ({fmt_ts(usr['sub_until'])})؛ اول تمدید کنید.")
        if self.db.active_count(uid) + len(proj) > self.max_active():
            ok = False
            lines.append(f"{e('no')} با این برنامه از سقف {self.max_active()} تسک فعال رد می‌شوید.")
        lines.append(ui.quote(f"{e('shield')} ربات هرگز بیشتر از «قیمت قابل‌قبول × تعداد» نمی‌پردازد و همیشه همان مبلغی را "
                              "می‌فرستد که قرارداد می‌خواهد. تعداد فقط ممکن است کمتر شود (سقف ولت / عرضه / موجودی)، هرگز بیشتر."))
        p["step"] = "summary"
        rows = ([[btn("تأیید و مسلح کردن", "pa", style="success", icon="bolt")]] if ok else []) + \
               [[btn("ویرایش", "pb", icon="edit"), btn("انصراف", "px", style="danger", icon="no")]]
        await q.message.edit_text("\n".join(lines) + (f"\n\n{e('next')} <b>تأیید می‌کنید؟</b>" if ok
                                                      else f"\n\n{e('stop')} <b>برنامه قابل ثبت نیست.</b>"),
                                  parse_mode=ParseMode.HTML, reply_markup=kb(*rows))

    async def plan_arm(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        p = c.user_data.get("plan")
        if not p or p.get("step") != "summary":
            if p and p.get("step") == "arming":
                return
            return await self.alert(q, "این برنامه قبلاً ثبت شده یا منقضی شده است.")
        if (m := self.need_sub(uid)):
            return await self.say(q.message, m)
        p["step"] = "arming"                                     # double taps hit the guard above
        sel = sorted((s for s in p["stages"] if s["qty"]), key=lambda s: s["start"])
        pref = self.db.prefs(uid)
        items = []
        for s in sel:
            lo, hi = self.unit_bounds(s["price_wei"], pref["price_policy"], pref["max_increase_pct"])
            items.append({"label": s["label"], "start": s["start"], "end": s["end"], "kind": s["kind"],
                          "price_wei": s["price_wei"], "quantity": s["qty"], "policy": pref["price_policy"], "uuid": s["uuid"],
                          "min_unit": lo, "max_unit": hi})
        plan_id = os.urandom(6).hex()
        res = self.db.add_plan(uid, p["wid"], p["slug"], p["nft"], items, self.max_active(), plan_id)
        if res in ("dup", "limit"):
            p["step"] = "summary"
            return await self.say(q.message, f"{e('stop')} " + ("برای یکی از این مرحله‌ها با این ولت قبلاً تسک فعال دارید."
                                                                if res == "dup" else "سقف تسک فعال پر است."))
        self.db.audit(uid, "plan_add", f"{plan_id} {p['slug']} " + ", ".join(f"{t}:{s['label']}x{s['qty']}" for t, s in zip(res, sel)))
        c.user_data.pop("plan", None)
        rows = []
        for t, s in zip(res, sel):
            prep = "همین الان آماده می‌شود" if s["start"] <= time.time() + self.cfg.spawn_before_s else \
                f"{self.cfg.spawn_before_s // 60} دقیقه قبل از شروع آماده می‌شود"
            rows.append(f"{e('bolt')} <b>تسک #{t}</b> · {esc(s['label'])} × {s['qty']}\n     {fmt_ts(s['start'])}\n     <i>{prep}</i>")
        try:                                                     # the summary screen becomes a read-only record
            await q.message.edit_reply_markup(kb([btn("ثبت شد", disabled=True, style="success", icon="ok")]))
        except Exception:  # noqa: BLE001
            pass
        await q.message.reply_text(f"{e('fire')} <b>مسلح شد!</b>\nربات منتظر لحظهٔ شروع هر مرحله می‌ماند و همان لحظه شلیک می‌کند:\n\n"
                                   + "\n\n".join(rows) + f"\n\n{e('bell')} نتیجهٔ هر مرحله همین‌جا اطلاع داده می‌شود.",
                                   parse_mode=ParseMode.HTML, reply_markup=self.menu(), message_effect_id=ui.EFFECT_FIRE)

    async def plan_cancel(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        c.user_data.pop("plan", None); c.user_data.pop("state", None)
        try:
            await q.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        await self.say(q.message, f"{e('stop')} لغو شد.", self.menu())

    async def input_cancel(self, u: Update, c):
        """Inline 'cancel' on prompts that wait for typed input (wallet key / API key / link)."""
        q = u.callback_query
        await self.safe_answer(q, "لغو شد")
        c.user_data.pop("state", None)
        c.user_data.pop("plan", None)
        try:
            await q.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        await self.say(q.message, f"{e('stop')} لغو شد.", self.menu())

    async def stale(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        await self.say(q.message, f"{e('info')} این دکمه مربوط به نسخهٔ قبلی است؛ دوباره «🎯 تسک جدید» را بزنید.")

    # ------------------------------------------------------------------ settings
    def settings_view(self, uid):
        pref = self.db.prefs(uid)
        txt = (f"{title('gear', 'تنظیمات')}\n\n{e('usd')} <b>واکنش به تغییر قیمت</b>\n"
               "<i>اگر سازنده هنگام شروع یا حین مینت قیمت را عوض کند:</i>\n"
               f"{e('pin')} فعلی: <b>{esc(self.policy_line(pref))}</b>\n\n"
               + ui.quote(
                   "<b>فقط همان قیمت</b> — هر تغییری ← هیچ تراکنشی ارسال نمی‌شود (امن‌ترین).\n"
                   "<b>کمتر شد مینت کن</b> — ارزان‌تر شد ← مینت، گران‌تر شد ← بیخیال.\n"
                   "<b>تا سقف ٪</b> — تا درصدی که تعیین می‌کنید گران‌تر هم مینت می‌کند؛ ارزان‌تر شدن ← بیخیال.\n"
                   "<b>هر تغییری تا سقف ٪</b> — ارزان‌تر یا گران‌تر (تا سقف) ← مینت.", expandable=True)
               + f"\n{e('shield')} ربات همیشه دقیقاً همان مبلغی را می‌فرستد که قرارداد می‌خواهد و هرگز بیشتر از سقف. "
               "تنظیم روی برنامه‌هایی اعمال می‌شود که از این به بعد مسلح می‌کنید.\n\n"
               f"{e('chart')} سقف تسک فعال: {self.max_active()}")
        cur = pref["price_policy"]
        mk = lambda key, label: (btn(label, f"cp:{key}", style="success", icon="ok") if cur == key
                                 else btn(label, f"cp:{key}"))
        rows = [[mk("exact", "فقط همان قیمت"), mk("lower_ok", "کمتر شد مینت کن")],
                [mk("cap", "تا سقف ٪ گران‌تر"), mk("any_up", "هر تغییری تا سقف ٪")]]
        if cur in ("cap", "any_up"):
            rows.append([btn(f"+{n}٪", f"cpp:{n}", style="primary") if pref["max_increase_pct"] == n
                         else btn(f"+{n}٪", f"cpp:{n}") for n in (5, 10, 25, 50, 100)])
        return txt, kb(*rows)

    async def settings(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        txt, mk = self.settings_view(uid)
        await self.say(u, txt, mk)

    async def set_policy(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        pref = self.db.prefs(uid)
        if q.data.startswith("cpp:"):
            pct = int(q.data.split(":")[1])
            if pct not in (5, 10, 25, 50, 100) or pref["price_policy"] not in ("cap", "any_up"):
                return
            self.db.set_prefs(uid, pref["price_policy"], pct)
        else:
            pol = q.data.split(":")[1]
            if pol not in POLICY_FA:
                return
            pct = pref["max_increase_pct"] or (10 if pol in ("cap", "any_up") else 0)
            self.db.set_prefs(uid, pol, pct if pol in ("cap", "any_up") else 0)
        self.db.audit(uid, "price_policy", json.dumps(self.db.prefs(uid)))
        txt, mk = self.settings_view(uid)
        await self.show(q, txt, mk)

    async def setmax(self, u: Update, c):
        if not self.is_admin(u):
            return
        try:
            n = int(c.args[0])
            assert 1 <= n <= 500
        except (IndexError, ValueError, AssertionError):
            return await self.say(u, f"<code>/setmax &lt;1..500&gt;</code>  (فعلی: {self.max_active()})")
        self.db.set_setting("max_active_tasks", n)
        self.db.audit(u.effective_user.id, "admin_setmax", str(n))
        await self.say(u, f"{e('ok')} ok, max active tasks = {n}")

    def tasks_view(self, uid):
        ts = self.db.tasks(uid)
        if not ts:
            return (f"{title('list', 'تسک‌های شما')}\n\n{e('info')} هنوز تسکی ندارید. لینک کالکشن را بفرستید یا «🎯 تسک جدید» را بزنید.",
                    kb([btn("تسک جدید", "nt", style="primary", icon="bolt")]))
        act = [t for t in ts if t["status"] in ("scheduled", "running")]
        done = [t for t in ts if t["status"] not in ("scheduled", "running")][:8]
        lines = [f"{title('list', 'تسک‌های شما')}  <i>({len(act)} فعال)</i>"]
        rows = []
        for group, head in ((act, "فعال"), (done, "اخیر")):
            if not group:
                continue
            lines.append(f"\n<b>{head}</b>")
            for t in group:
                ic, lab = STATUS.get(t["status"], ("info", t["status"]))
                lines.append(f"{e(ic)} <b>#{t['id']}</b> {esc(t['slug'])} · {esc(t['stage_label'])} × {t['quantity']}\n"
                             f"     {lab} · {fmt_ts(t['start_ts'])}")
                row = [btn(f"#{t['id']} جزئیات", f"td:{t['id']}", icon="search")]
                if t["status"] in ("scheduled", "running"):
                    row.append(btn(f"لغو #{t['id']}", f"tcq:{t['id']}", style="danger", icon="stop"))
                elif t["tx_hash"]:
                    row.append(btn("تراکنش", url=ui.tx_url(t["tx_hash"]), icon="link"))
                rows.append(row)
        rows.append([btn("بروزرسانی", "tl", icon="refresh")])
        return "\n".join(lines), kb(*rows)

    async def tasks(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        txt, mk = self.tasks_view(uid)
        await self.say(u, txt, mk)

    async def tasks_refresh(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q, "بروزرسانی شد")
        await self.show(q, *self.tasks_view(q.from_user.id))

    async def task_detail(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        t = self.db.task(int(q.data.split(":")[1]))
        if not t or t["user_id"] != q.from_user.id:
            return await self.show(q, *self.tasks_view(q.from_user.id))
        ic, lab = STATUS.get(t["status"], ("info", t["status"]))
        w = self.db.wallet(t["user_id"], t["wallet_id"])
        lines = [f"{e(ic)} <b>تسک #{t['id']}</b> — {lab}", ui.SEP,
                 ui.kv("کالکشن", esc(t["slug"])), ui.kv("مرحله", f"{esc(t['stage_label'])} · {self.kind_fa(t['kind'])}"),
                 ui.kv("تعداد", t["quantity"]), ui.kv("شروع", fmt_ts(t["start_ts"]))]
        if w:
            lines.append(ui.kv("ولت", ui.code(w["address"])))
        if t["result"]:
            lines.append("\n" + ui.quote(esc(t["result"]), expandable=len(t["result"]) > 200))
        rows = []
        if t["tx_hash"]:
            rows.append([btn("مشاهده در اکسپلورر", url=ui.tx_url(t["tx_hash"]), icon="search"),
                         btn("کپی tx", copy=t["tx_hash"], icon="link")])
        if t["status"] in ("scheduled", "running"):
            rows.append([btn("لغو این تسک", f"tcq:{t['id']}", style="danger", icon="stop")])
        rows.append([btn("برگشت به لیست", "tl", icon="next")])
        await self.show(q, "\n".join(lines), kb(*rows))

    async def task_cancel_ask(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        t = self.db.task(int(q.data.split(":")[1]))
        if not t or t["user_id"] != q.from_user.id or t["status"] not in ("scheduled", "running"):
            await self.alert(q, "این تسک دیگر قابل لغو نیست.")
            return await self.show(q, *self.tasks_view(q.from_user.id))
        await self.show(q, f"{e('warn')} <b>لغو تسک #{t['id']}؟</b>\n{esc(t['slug'])} · {esc(t['stage_label'])} × {t['quantity']}\n"
                           f"{fmt_ts(t['start_ts'])}\n\nبعد از لغو، ربات برای این مرحله شلیک نمی‌کند.",
                        kb([btn("بله، لغو کن", f"tc:{t['id']}", style="danger", icon="stop"),
                            btn("نه، برگرد", "tl", icon="next")]))

    async def task_cancel(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        tid = int(q.data.split(":")[1])
        ok = await self.blocking(self.runner.cancel, q.from_user.id, tid)
        txt, mk = self.tasks_view(q.from_user.id)
        await self.show(q, (f"{e('ok')} تسک #{tid} لغو شد.\n\n" if ok else f"{e('info')} تسک #{tid} قابل لغو نیست.\n\n") + txt, mk)

    async def new_task_cb(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        await self.new_task(u, c)

    # ------------------------------------------------------------------ text router
    async def text(self, u: Update, c):
        t = u.message.text
        routes = {B_SUB: self.sub, B_WAL: self.wallets, B_KEY: self.key, B_NEW: self.new_task, B_TASKS: self.tasks,
                  B_HELP: self.help, B_SET: self.settings}
        st = c.user_data.get("state")
        if st == "await_pk":                    # checked FIRST: a key message must never be echoed/routed
            return await self.got_pk(u, c)
        if t in routes:
            for k in ("state", "plan"):                                    # leaving the task flow
                c.user_data.pop(k, None)
            return await routes[t](u, c)
        if st == "await_key":
            return await self.got_key(u, c)
        if st == "await_link":
            return await self.got_link(u, c)
        if st == "await_qty":
            return await self.got_qty(u, c)
        step = (c.user_data.get("plan") or {}).get("step")
        hint = {"wallet": "ولت را از دکمه‌های پیام قبلی انتخاب کنید",
                "checking": "در حال پیش‌بررسی ولت؛ چند ثانیه صبر کنید",
                "select": "مرحله‌ها را با دکمه‌های پیام قبلی انتخاب کنید و بعد «ادامه» را بزنید",
                "qty": "تعداد را با دکمه‌ها انتخاب کنید (برای عدد دلخواه «عدد دلخواه» را بزنید)",
                "summary": "برنامه هنوز ثبت نشده؛ «تأیید و مسلح کردن» یا «انصراف» را بزنید",
                "arming": "در حال ثبت برنامه…"}.get(step)
        if hint:
            return await self.say(u, f"{e('info')} {hint} (یا /cancel).")
        # a pasted private key outside the flow: delete it anyway
        if len(t) in (64, 66) and all(ch in "0123456789abcdefABCDEFx" for ch in t):
            try:
                await u.message.delete()
            except Exception:  # noqa: BLE001
                pass
            return await u.effective_chat.send_message(
                f"{e('siren')} پیام شبیه کلید خصوصی بود و <b>پاک شد</b>. برای ثبت ولت از «👛 ولت‌ها» اقدام کنید.",
                parse_mode=ParseMode.HTML)
        # A pasted OpenSea link (or forwarded text with one) starts a new task directly
        if self.extract_link(t) and ("opensea.io" in t.lower()):
            uid = u.effective_user.id
            self.db.ensure_user(uid, u.effective_user.username)
            if not self.need_sub(uid) and self.db.apikey(uid) and self.db.wallets(uid) \
                    and self.db.active_count(uid) < self.max_active():
                return await self.got_link(u, c)
            return await self.new_task(u, c)
        await self.say(u, f"{e('info')} از دکمه‌های منو استفاده کنید یا لینک کالکشن OpenSea را بفرستید.", self.menu())

    # ------------------------------------------------------------------ admin
    def is_admin(self, u):
        return u.effective_user.id in self.cfg.admin_ids

    async def grant(self, u: Update, c):
        if not self.is_admin(u):
            return
        try:
            uid, days = int(c.args[0]), float(c.args[1])
        except (IndexError, ValueError):
            return await self.say(u, "<code>/grant &lt;user_id&gt; &lt;days&gt;</code>")
        new = self.db.extend_sub(uid, days)
        self.db.audit(u.effective_user.id, "admin_grant", f"{uid} {days}")
        await self.say(u, f"{e('ok')} ok, until {fmt_ts(new)}")

    async def ban(self, u: Update, c):
        if not self.is_admin(u):
            return
        try:
            uid, v = int(c.args[0]), int(c.args[1]) if len(c.args) > 1 else 1
        except (IndexError, ValueError):
            return await self.say(u, "<code>/ban &lt;user_id&gt; [1|0]</code>")
        self.db.ensure_user(uid)
        self.db.x("UPDATE users SET banned=? WHERE id=?", (v, uid))
        await self.say(u, f"{e('ok')} ok")

    async def stats(self, u: Update, c):
        if not self.is_admin(u):
            return
        q = lambda s, a=(): self.db.q(s, a)[0][0]
        now = int(time.time())
        active = q("SELECT COUNT(*) FROM tasks WHERE status IN ('scheduled','running')")
        minted = q("SELECT COUNT(*) FROM tasks WHERE status='minted'")
        failed = q("SELECT COUNT(*) FROM tasks WHERE status='failed'")
        await self.say(u,
            f"{title('chart', 'آمار')}\n"
            f"users {q('SELECT COUNT(*) FROM users')} | active subs {q('SELECT COUNT(*) FROM users WHERE sub_until>?', (now,))}\n"
            f"tasks active {active} | minted {minted} | failed {failed}\n"
            f"stars {q('SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0')}")

    # ------------------------------------------------------------------ wiring
    def build(self) -> Application:
        # Telegram default read timeout is 5 s; a live ReadTimeout (2026-10-21) killed a handler -> more headroom.
        # SafeBot = ExtBot that retries without premium-only content if Telegram ever rejects it (see ui.py).
        base = (self.cfg.telegram_base_url.rstrip("/") if self.cfg.telegram_base_url else "https://api.telegram.org")
        bot = ui.SafeBot(
            token=self.cfg.bot_token, base_url=base + "/bot", base_file_url=base + "/file/bot",
            request=HTTPXRequest(connection_pool_size=256, read_timeout=20, write_timeout=20, connect_timeout=10, pool_timeout=5),
            get_updates_request=HTTPXRequest(connection_pool_size=1, read_timeout=20, write_timeout=20, connect_timeout=10,
                                             pool_timeout=5))
        # NOTE: no telegram.ext.Defaults on purpose: in PTB 22.5 ExtBot._insert_defaults turns provider_token=""
        # (Telegram Stars invoices) into [] when any Defaults object is set (empty str is a Sequence, all() of
        # nothing is True). Link previews are disabled per message instead.
        b = ApplicationBuilder().bot(bot).concurrent_updates(True)
        b = b.post_init(self._post_init).post_shutdown(self._post_shutdown)
        app = b.build()
        P = PRIVATE
        app.add_handler(CommandHandler("start", self.start, filters=P))
        app.add_handler(CommandHandler("help", self.help, filters=P))
        app.add_handler(CommandHandler("cancel", self.cancel_cmd, filters=P))
        app.add_handler(CommandHandler("wallets", self.wallets, filters=P))
        app.add_handler(CommandHandler("key", self.key, filters=P))
        app.add_handler(CommandHandler("tasks", self.tasks, filters=P))
        app.add_handler(CommandHandler("sub", self.sub, filters=P))
        app.add_handler(CommandHandler("grant", self.grant, filters=P))
        app.add_handler(CommandHandler("ban", self.ban, filters=P))
        app.add_handler(CommandHandler("stats", self.stats, filters=P))
        app.add_handler(CommandHandler("setmax", self.setmax, filters=P))
        app.add_handler(CommandHandler("settings", self.settings, filters=P))
        app.add_handler(CommandHandler("new", self.new_task, filters=P))
        app.add_handler(CallbackQueryHandler(self.buy, pattern=r"^buy$"))
        app.add_handler(CallbackQueryHandler(self.wallet_add, pattern=r"^wadd$"))
        app.add_handler(CallbackQueryHandler(self.wallet_del, pattern=r"^wdel:\d+$"))
        app.add_handler(CallbackQueryHandler(self.wallet_del_ask, pattern=r"^wdq:\d+$"))
        app.add_handler(CallbackQueryHandler(self.wallets_back, pattern=r"^wls$"))
        app.add_handler(CallbackQueryHandler(self.key_set, pattern=r"^kset$"))
        app.add_handler(CallbackQueryHandler(self.key_del, pattern=r"^kdel:\d+$"))
        app.add_handler(CallbackQueryHandler(self.key_del_ask, pattern=r"^kdq:\d+$"))
        app.add_handler(CallbackQueryHandler(self.keys_back, pattern=r"^kls$"))
        app.add_handler(CallbackQueryHandler(self.input_cancel, pattern=r"^xc$"))
        app.add_handler(CallbackQueryHandler(self.new_task_cb, pattern=r"^nt$"))
        app.add_handler(CallbackQueryHandler(self.tasks_refresh, pattern=r"^tl$"))
        app.add_handler(CallbackQueryHandler(self.task_detail, pattern=r"^td:\d+$"))
        app.add_handler(CallbackQueryHandler(self.task_cancel_ask, pattern=r"^tcq:\d+$"))
        app.add_handler(CallbackQueryHandler(self.set_policy, pattern=r"^cpp?:[a-z_0-9]+$"))
        app.add_handler(CallbackQueryHandler(self.plan_wallet, pattern=r"^pw:\d+$"))
        app.add_handler(CallbackQueryHandler(self.plan_stage, pattern=r"^ps:\d+$"))
        app.add_handler(CallbackQueryHandler(self.plan_qty, pattern=r"^pq:\d+:(\d+|max|c)$"))
        app.add_handler(CallbackQueryHandler(self.plan_back, pattern=r"^pb$"))
        app.add_handler(CallbackQueryHandler(self.plan_next, pattern=r"^pn$"))
        app.add_handler(CallbackQueryHandler(self.plan_arm, pattern=r"^pa$"))
        app.add_handler(CallbackQueryHandler(self.plan_cancel, pattern=r"^px$"))
        app.add_handler(CallbackQueryHandler(self.stale, pattern=r"^(st:\d+|tw:\d+|tok|tno)$"))
        app.add_handler(CallbackQueryHandler(self.task_cancel, pattern=r"^tc:\d+$"))
        app.add_handler(PreCheckoutQueryHandler(self.precheckout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, self.paid))
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA & P, self.webapp_data))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & P, self.text))
        app.add_error_handler(self._on_error)
        self.app = app
        return app

    async def _on_error(self, update, c):
        log.error("handler error: %s: %s", type(c.error).__name__, str(c.error)[:300], exc_info=c.error)
        try:
            if update and getattr(update, "effective_chat", None):
                await c.bot.send_message(update.effective_chat.id,
                                         f"{e('warn')} <b>مشکلی پیش آمد</b> — لطفاً دوباره تلاش کنید.\n"
                                         "<i>اگر تکرار شد /cancel بزنید و از اول شروع کنید.</i>", parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass

    COMMANDS = [("start", "شروع و وضعیت حساب"), ("new", "تسک مینت جدید"), ("tasks", "تسک‌های من"),
                ("wallets", "ولت‌ها"), ("key", "کلیدهای API"), ("settings", "تنظیمات"), ("sub", "اشتراک"),
                ("help", "راهنما"), ("cancel", "لغو کار فعلی")]

    async def _post_init(self, app):
        self.loop = asyncio.get_running_loop()
        self._runner_task = asyncio.create_task(self.runner.loop())
        asyncio.create_task(self._set_profile(app.bot))

    async def _set_profile(self, bot):
        """Command menu + profile texts (idempotent; failures never affect the bot)."""
        try:
            await bot.set_my_commands([BotCommand(c, d) for c, d in self.COMMANDS])
            await bot.set_my_short_description("مینت خودکار و فوق‌سریع دراپ‌های OpenSea روی Robinhood Chain ⚡")
            await bot.set_my_description(
                "Ray Minter ⚡ مینت خودکار دراپ‌های OpenSea روی Robinhood Chain\n\n"
                "• Allowlist و Public، چند مرحله با تعداد دلخواه\n• شلیک دقیقاً در لحظهٔ شروع هر مرحله\n"
                "• چک allowlist و سقف هر ولت قبل از ثبت\n• گزارش سرعت و رتبه بعد از هر مینت\n\n"
                "برای شروع «Start» را بزنید.")
        except Exception as ex:  # noqa: BLE001
            log.warning("profile setup skipped: %s", type(ex).__name__)

    async def _post_shutdown(self, app):
        t = getattr(self, "_runner_task", None)
        if t:
            t.cancel()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx INFO logs the URL, which contains the bot token
    cfg = Config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN missing")
    svc = Service(cfg)
    svc.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
