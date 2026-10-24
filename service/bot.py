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

from telegram import BotCommand, LabeledPrice, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from . import preflight as pf
from . import rich as R
from . import ui
from .rich import Doc, lt, ltr, mono
from .ui import btn, e, kb, title, when
from .config import Config
from .db import DB
from .opensea import OpenSea, OpenSeaError, norm_uuid
from .runner import Runner
from . import ton_pay as TP
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
        self.ton = TP.TonGateway(self.db, cfg, on_event=self._ton_event)
        self.runner = Runner(self.db, self.vault, cfg, self.notify, extra_runner_args, admin_notify=self.notify_admins,
                             notify_rich=self.notify_rich)

    # ------------------------------------------------------------------ infra
    def notify(self, uid, text, html_=False, markup=None, effect=None):
        """Thread-safe (runner runs in an executor thread). html_=True: text is already HTML-escaped;
        text may also be a rich.Doc (sent as an RTL rich message)."""
        if self.loop and self.app:
            asyncio.run_coroutine_threadsafe(self._send(uid, text, html_, markup, effect), self.loop)

    def notify_rich(self, uid, doc, markup=None, effect=None):
        self.notify(uid, doc, html_=True, markup=markup, effect=effect)

    def notify_admins(self, text, markup=None):
        for a in self.cfg.admin_ids:
            self.notify(a, text, markup=markup)

    async def _send(self, uid, text, html_=False, markup=None, effect=None):
        try:
            if isinstance(text, Doc):
                await R.send(self.app.bot, uid, text, markup, effect)
            elif html_:
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

    async def say(self, u_or_msg, text, markup=None, effect=None, **kw):
        """Send to the same chat. `text` may be a rich.Doc (RTL rich message) or a classic HTML string."""
        m = u_or_msg.effective_message if isinstance(u_or_msg, Update) else u_or_msg
        if isinstance(text, Doc):
            return await R.send(m.get_bot(), m.chat_id, text, markup, effect)
        return await m.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, message_effect_id=effect, **kw)

    async def show(self, q, text, markup=None):
        """Edit the message the inline button belongs to (one living screen instead of a stream of messages);
        falls back to a new message if it can't be edited (too old / not ours)."""
        return await self.screen(q.message, text, markup)

    async def screen(self, msg, text, markup=None):
        """Edit `msg` in place with `text` (rich.Doc or classic HTML); new message if editing is impossible."""
        try:
            if isinstance(text, Doc):
                r = await R.edit(msg.get_bot(), msg.chat_id, msg.message_id, text, markup)
                return r if r not in (None, True) else msg
            return await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as ex:  # noqa: BLE001
            if "not modified" in str(ex).lower():
                return msg
            log.info("edit failed (%s) -> new message", str(ex)[:80])
            return await self.say(msg, text, markup)

    async def alert(self, q, text, show_alert=False):
        await self.safe_answer(q, text[:190], show_alert=show_alert)

    def progress_card(self, uid):
        """Onboarding checklist: subscription -> wallet -> API key -> link. -> (done_count, [(bool, html)])"""
        usr = self.db.user(uid)
        sub = bool(usr) and self.db.is_subscribed(uid)
        wal = bool(self.db.wallets(uid))
        key = bool(self.db.apikeys(uid, valid_after=time.time()))
        items = [(sub, "<b>اشتراک</b>" + (f" — فعال تا {R.tm(usr['sub_until'], 'D')}" if sub else " — هنوز فعال نشده")),
                 (wal, "<b>ولت burner</b> — یک ولت جداگانه برای مینت"),
                 (key, f"<b>کلید {lt('API')} شخصی</b> {lt('OpenSea')}"),
                 (False, "<b>لینک کالکشن</b> را بفرستید و برنامه را مسلح کنید")]
        return sum((sub, wal, key)), items

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
        """ONE short, centered message (pull quotes). No sales push: a subscription is offered only when the user
        opens «💳 اشتراک» or uses a feature that needs one. The next onboarding step is a text hint in the same message."""
        usr = u.effective_user
        self.db.ensure_user(usr.id, usr.username)
        c.user_data.clear()
        name = esc(usr.first_name or "دوست عزیز")
        d = Doc().note(f"<b>سلام {name}، به {lt('Ray Minter')} خوش آمدید</b><br>"
                       f"مینت خودکار دراپ‌های {lt('OpenSea')} روی {lt('Robinhood Chain')}<br>"
                       "در همان ثانیهٔ شروع هر مرحله، سریع و امن", "robot", cite=lt("Ray Minter"))
        if not self.db.is_subscribed(usr.id):
            step = "برای شروع، از منوی پایین بخش‌ها را ببینید"
        elif not self.db.wallets(usr.id):
            step = "قدم بعدی: افزودن ولت از «👛 ولت‌ها»"
        elif not self.db.apikeys(usr.id, valid_after=time.time()):
            step = f"قدم بعدی: افزودن کلید {lt('API')} از «🔑 کلید API»"
        else:
            step = "لینک کالکشن را بفرستید یا «🎯 تسک جدید» را بزنید"
        d.note(f"{step}<br>راهنما: {lt('/help')}", "next")
        await self.say(u, d, self.menu())

    async def help(self, u: Update, c):
        d = (Doc().h("راهنمای Ray Minter", "info", 2)
             .h("چطور کار می‌کند", "next", 3)
             .ol([f"لینک کالکشن {lt('OpenSea')} را بفرستید (یا «🎯 تسک جدید»).",
                  "ولتی را که با آن مینت می‌کنید انتخاب کنید.",
                  "مرحله‌ها و تعداد هرکدام را مشخص کنید.",
                  "«تأیید و مسلح کردن» — ربات در لحظهٔ شروع هر مرحله شلیک می‌کند و نتیجه را همین‌جا می‌فرستد."])
             .hr()
             .details(f"{e('pin')} نکات مهم دربارهٔ مرحله‌ها و سقف", "<ul>"
                      f"<li>دراپ‌های {lt('SeaDrop')} روی {lt('Robinhood Chain')}: مرحله‌های {lt('Allowlist')} (امضادار) و {lt('Public')}، رایگان یا پولی.</li>"
                      f"<li>قبل از ثبت، {lt('allowlist')} و سقف هر مرحله برای همان ولت از {lt('OpenSea')} (ورود {lt('SIWE')}) چک می‌شود.</li>"
                      f"<li>سقف هر ولت ({lt('Limit N per wallet')}) <b>جدا برای هر مرحله</b> است؛ مثلاً {lt('Limit 4')} در مرحلهٔ اول و "
                      f"{lt('Limit 1')} در مرحلهٔ دوم یعنی جمعاً ۵ عدد.</li>"
                      "<li>ربات هرگز بیشتر از سقف، عرضه یا قیمت قابل‌قبول شما ارسال نمی‌کند.</li></ul>")
             .details(f"{e('gear')} امکانات", "<ul>"
                      "<li><b>واکنش به تغییر قیمت</b> در «⚙️ تنظیمات»: فقط همان قیمت / کمتر شد / تا سقف ٪ / هر تغییری تا سقف ٪.</li>"
                      f"<li><b>چند کلید {lt('API')}</b>: هر مرحلهٔ هم‌زمان کلید اختصاصی می‌گیرد تا سهمیهٔ {lt('OpenSea')} تقسیم نشود.</li>"
                      "<li><b>گزارش سرعت</b> (امضا، ارسال، تأیید) و <b>رتبهٔ شما</b> بین مینت‌کننده‌ها بعد از هر مینت.</li>"
                      "<li>کلیدهای فوری OpenSea ۷ روزه‌اند؛ ربات قبل از انقضا هشدار می‌دهد.</li></ul>")
             .details(f"{e('shield')} امنیت", "<ul>"
                      "<li>فقط یک <b>ولت جداگانه (burner)</b> با کمی ETH بسازید و NFT را بعد از مینت منتقل کنید.</li>"
                      f"<li>کلید خصوصی بلافاصله از چت پاک و با {lt('AES-256-GCM')} رمزنگاری می‌شود.</li>"
                      "<li><b>هرگز کلید ولت اصلی خود را وارد نکنید.</b></li></ul>", open_=True)
             .hr()
             .table(["دستور", "کار"], [[lt("/new"), "تسک مینت جدید"], [lt("/tasks"), "تسک‌های من"],
                                       [lt("/wallets"), "ولت‌ها"], [lt("/key"), f"کلیدهای {lt('API')}"],
                                       [lt("/settings"), "تنظیمات"], [lt("/sub"), "اشتراک"], [lt("/cancel"), "لغو کار فعلی"]],
                    caption="دستورات"))
        await self.say(u, d)

    async def cancel_cmd(self, u: Update, c):
        c.user_data.clear()
        await self.say(u, Doc().p(f"{e('stop')} <b>لغو شد.</b> هر وقت خواستید از منو ادامه دهید."), self.menu())

    # ------------------------------------------------------------------ subscription
    async def sub(self, u: Update, c):
        usr = self.db.ensure_user(u.effective_user.id, u.effective_user.username)
        price = f"{ltr(self.cfg.price_stars)} ⭐ {lt('Stars')}"
        if self.ton.enabled():
            price += f" یا {ltr(TP.fmt_ton(self.ton.price_nano()) + ' TON')}"
        plan = [("مدت", f"{self.cfg.sub_days} روز"), ("قیمت", price),
                ("تسک فعال هم‌زمان", f"تا {ltr(self.max_active())}"), ("ولت", "تا ۵ ولت")]
        if self.db.is_subscribed(usr["id"]):
            left = max(0, usr["sub_until"] - time.time()) / 86400
            d = (Doc().h("اشتراک شما", "gem", 2)
                 .kv([("وضعیت", f"{e('green')} <b>فعال</b>"), ("باقی‌مانده", f"{ltr(f'{left:.1f}')} روز"),
                      ("پایان", R.when(usr["sub_until"]))])
                 .note(f"تمدید، {self.cfg.sub_days} روز به <b>انتهای</b> اشتراک فعلی اضافه می‌کند.", "idea"))
            renew = True
        else:
            d = (Doc().h(f"اشتراک {lt('Ray Minter')}", "gem", 2)
                 .p(f"{e('red')} اشتراک فعال ندارید.")
                 .checklist([(True, f"مینت خودکار {lt('Allowlist')} و {lt('Public')}، چند مرحله و چند ولت"),
                             (True, "شلیک در همان ثانیهٔ شروع هر مرحله"),
                             (True, "گزارش سرعت و رتبه بعد از هر مینت"),
                             (True, "واکنش هوشمند به تغییر قیمت")])
                 .kv(plan, caption="پلن")
                 .footer(f"پرداخت با {lt('Telegram Stars')} داخل تلگرام" + (
                     f" یا با {lt('TON')} از هر کیف‌پول و صرافی" if self.ton.enabled() else "")))
            renew = False
        await self.say(u, d, kb(*self.pay_rows(renew)))

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
            d = (Doc().h("پرداخت موفق — خوش آمدید!", "party", 2)
                 .kv([("اشتراک", f"{e('green')} فعال"), ("تا", R.when(new)), ("مبلغ", f"{ltr(p.total_amount)} ⭐")])
                 .p(f"{e('next')} <b>قدم بعدی:</b> ولت burner و کلید {lt('API')} را ثبت کنید، بعد لینک کالکشن را بفرستید."))
            await self.say(u, d, self.menu(), effect=ui.EFFECT_PARTY)

    # ------------------------------------------------------------------ TON payments
    def ton_invoice_doc(self, inv, rate=None):
        """ONE compact message: QR + amount / address / memo + 3 short steps. Details stay behind a collapsed block."""
        a = self.ton.address
        amt = TP.fmt_ton(inv["amount_nano"])
        links = TP.transfer_links(a, inv["amount_nano"], inv["memo"])
        usd = ""
        if rate:
            usd = " ≈ " + ltr("$" + format(rate * inv["amount_nano"] / TP.NANO, ",.2f"))
        paid = inv["paid_nano"] - inv["periods"] * inv["price_nano"]
        d = (Doc().h(f"پرداخت با {lt('TON')} — {ltr(inv['days'])} روز", "coin", 3)
             .image("qr", TP.qr_png(links["ton"]))
             .kv([("مبلغ", f"<b>{mono(amt)}</b> {lt('TON')}{usd}"),
                  ("آدرس", mono(a)),
                  ("کامنت", f"<b>{mono(inv['memo'])}</b>"),
                  ("پرداخت‌شده", f"{ltr(TP.fmt_ton(paid))} {lt('TON')}") if paid > 0 else None])
             .note("QR را اسکن کنید یا «باز کردن در کیف‌پول» را بزنید. از صرافی: آدرس، مبلغ و کامنت را کپی کنید.<br>"
                   "بعد از پرداخت، اشتراک <b>خودکار</b> فعال می‌شود.", None)
             .details("راهنما", "<ul><li>اگر صرافی کامنت نمی‌گیرد، فقط <b>دقیقاً همین مبلغ</b> را بفرستید (تا "
                      f"{R.tm(inv['expires'], 't')}).</li><li>کمتر فرستادید؟ باقی را با همان کامنت بفرستید.</li>"
                      f"<li>فقط {lt('TON')} روی شبکهٔ {lt('TON')}.</li></ul>"))
        mk = kb([btn("باز کردن در کیف‌پول", url=links["tonkeeper"], style="success", icon="coin")],
                [btn("آدرس", copy=a, icon="link"), btn("مبلغ", copy=amt, icon="usd"), btn("کامنت", copy=inv["memo"], icon="edit")],
                [btn("بررسی پرداخت", f"tonchk:{inv['id']}", style="primary", icon="refresh")])
        return d, mk, links

    async def ton_pay(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        uid = q.from_user.id
        if not self.ton.enabled():
            return await self.say(q.message, Doc().p(f"{e('warn')} پرداخت با {lt('TON')} فعلاً در دسترس نیست."))
        self.db.ensure_user(uid, q.from_user.username)
        try:
            inv = self.ton.invoice_for(uid)
        except RuntimeError as ex:
            log.error("TON invoice failed: %s", ex)
            return await self.say(q.message, Doc().p(f"{e('warn')} ساخت فاکتور ممکن نشد؛ چند دقیقهٔ دیگر دوباره امتحان کنید."))
        d, mk, _ = self.ton_invoice_doc(inv, await self.ton.api.usd_rate())
        await self.say(q.message, d, mk)

    async def ton_check(self, u: Update, c):
        q = u.callback_query
        uid = q.from_user.id
        try:
            inv = self.db.ton_invoice(int(q.data.split(":")[1]))
        except (IndexError, ValueError):
            inv = None
        if not inv or inv["user_id"] != uid:
            return await self.alert(q, "فاکتور پیدا نشد.")
        before = inv["paid_nano"]
        await self.ton.poll_once(force=True)
        inv = self.db.ton_invoice(inv["id"])
        if inv["periods"] > 0 and inv["paid_nano"] > before:
            return await self.safe_answer(q, "پرداخت تأیید شد ✅")
        if inv["periods"] > 0:
            return await self.alert(q, "این فاکتور پرداخت شده و اشتراک فعال است ✅", True)
        if self.ton.healthy is False:
            return await self.alert(q, "اتصال به شبکهٔ TON موقتاً برقرار نیست؛ پرداخت شما بعداً خودکار بررسی می‌شود.", True)
        if inv["paid_nano"] > 0:
            need = (inv["periods"] + 1) * inv["price_nano"] - inv["paid_nano"]
            return await self.alert(q, f"{TP.fmt_ton(inv['paid_nano'])} TON دریافت شد؛ {TP.fmt_ton(need)} TON دیگر با همان کامنت بفرستید.", True)
        await self.alert(q, "هنوز پرداختی پیدا نشد. اگر الان فرستادید، تا تأیید شبکه صبر کنید؛ به‌محض دریافت خودکار خبر می‌دهیم.", True)

    def _ton_event(self, kind, data):
        """Called by the gateway (inside the poll loop) after a transaction has been committed / on API health changes."""
        if kind in ("api_down", "api_up"):
            return self._ton_admin(kind, data)
        inc = data["inc"]
        link = kb([btn("مشاهدهٔ تراکنش", url=TP.tx_url(inc["hash"]), icon="search")])
        if kind == "paid":
            d = (Doc().h("پرداخت TON تأیید شد — خوش آمدید!", "party", 2)
                 .kv([("اشتراک", f"{e('green')} فعال"), ("تا", R.when(data["until"])),
                      ("مبلغ دریافتی", f"{ltr(TP.fmt_ton(inc['value']))} {lt('TON')}"),
                      ("دوره", f"{ltr(data['periods'] * data['days'])} روز")])
                 .p(f"{e('next')} <b>قدم بعدی:</b> ولت burner و کلید {lt('API')} را ثبت کنید، بعد لینک کالکشن را بفرستید."))
            self.notify(data["uid"], d, markup=link, effect=ui.EFFECT_PARTY)
        elif kind == "partial":
            d = (Doc().h("بخشی از مبلغ دریافت شد", "wait", 3)
                 .kv([("دریافتی", f"{ltr(TP.fmt_ton(inc['value']))} {lt('TON')}"),
                      ("باقی‌مانده", f"<b>{ltr(TP.fmt_ton(data['need']))}</b> {lt('TON')}"),
                      ("کامنت", mono(data["memo"]))])
                 .note("باقی‌مانده را با <b>همان کامنت</b> بفرستید تا اشتراک فعال شود.", "info"))
            self.notify(data["uid"], d, markup=link)
        self._ton_admin(kind, data)

    def _ton_admin(self, kind, data):
        """Admin card for every gateway event (rich, with action buttons)."""
        head = {"paid": ("party", "پرداخت TON — اشتراک فعال شد"), "partial": ("wait", "پرداخت TON — ناقص"),
                "unmatched": ("siren", "پرداخت TON بدون فاکتور"), "api_down": ("warn", "TonAPI در دسترس نیست"),
                "api_up": ("ok", "TonAPI دوباره وصل شد")}[kind]
        d = Doc().h(head[1], head[0], 3)
        rows = []
        if kind in ("api_down", "api_up"):
            d.kv([("از", R.when(data["since"])),
                  ("خطا", lt(str(data.get("error"))[:120])) if kind == "api_down" else
                  ("مدت قطعی", ltr(f"{data['secs'] / 60:.1f} min"))])
            d.note("پرداخت‌ها گم نمی‌شوند؛ بعد از اتصال از همان نقطه بررسی می‌شوند." if kind == "api_down"
                   else "پرداخت‌های دوران قطعی دوباره بررسی شدند.", "info")
        else:
            inc = data["inc"]
            uid = data.get("uid")
            usr = self.db.user(uid) if uid else None
            who = ((f"{lt('@' + usr['username'])} · " if usr and usr["username"] else "") + mono(uid)) if uid else "نامشخص"
            memo = mono(inc["memo"]) if inc.get("memo") else ("🔒 رمزنگاری‌شده" if inc.get("encrypted") else "—")
            how = {"memo": "کامنت", "amount": "مبلغ یکتا"}.get(data.get("how"), "—")
            d.kv([("مبلغ", f"<b>{ltr(TP.fmt_ton(inc['value']))}</b> {lt('TON')}"),
                  ("کاربر", who),
                  ("تطبیق با", how) if uid else None,
                  ("کامنت", memo),
                  ("متن کامنت", lt((inc.get("comment") or "")[:60])) if inc.get("comment") and not inc.get("memo") else None,
                  ("اشتراک تا", R.when(data["until"])) if kind == "paid" else None,
                  ("باقی‌مانده", f"{ltr(TP.fmt_ton(data['need']))} {lt('TON')}") if kind == "partial" else None,
                  ("فرستنده", mono(TP.friendly_address(inc["source"]))) if inc.get("source") else None,
                  ("زمان", R.when(inc["utime"]))])
            if kind == "unmatched":
                d.note(f"این واریز به هیچ فاکتوری نخورد و <b>اعمال نشد</b>. اگر صاحبش را می‌شناسید: "
                       f"{mono('/tonassign ' + inc['hash'] + ' <user_id>')}", "warn")
            rows.append([btn("تراکنش", url=TP.tx_url(inc["hash"]), icon="search"), btn("کپی tx", copy=inc["hash"], icon="link")])
            if inc.get("source"):
                rows.append([btn("فرستنده در Tonviewer", url=f"https://tonviewer.com/{TP.friendly_address(inc['source'])}", icon="search")])
            # NOTE: no tg://user?id= URL button — live-tested: Telegram rejects the WHOLE message
            # (Button_user_invalid) when it can't resolve the user, which would silently drop the alert.
        self.notify_admins(d, kb(*rows) if rows else None)

    def pay_rows(self, renew=False):
        """Payment buttons — the SAME choice everywhere a subscription is offered (Stars + TON when enabled)."""
        rows = [[btn(f"{'تمدید' if renew else 'پرداخت'} با Stars — {self.cfg.price_stars} ⭐", "buy", style="success", icon="pstar")]]
        if self.ton.enabled():
            rows.append([btn(f"{'تمدید' if renew else 'پرداخت'} با TON — {TP.fmt_ton(self.ton.price_nano())} TON", "tonpay",
                             style="primary", icon="coin")])
        return rows

    def need_sub(self, uid):
        return None if self.db.is_subscribed(uid) else \
            Doc().p(f"{e('lock')} <b>برای این بخش اشتراک فعال لازم است.</b>").p("یکی از روش‌های پرداخت زیر را انتخاب کنید.")

    async def paywall(self, target, m):
        return await self.say(target, m, kb(*self.pay_rows()))

    # ------------------------------------------------------------------ wallets
    def wallets_view(self, uid):
        ws = self.db.wallets(uid)
        d = Doc().h(f"ولت‌های شما — {ltr(f'{len(ws)}/5')}", "coin", 2)
        if ws:
            rows = []
            for n, w in enumerate(ws, 1):
                busy = self.db.q("SELECT COUNT(*) FROM tasks WHERE wallet_id=? AND status IN ('scheduled','running')", (w["id"],))[0][0]
                rows.append([ltr(n), mono(w["address"]), f"{e('bolt')} {ltr(busy)}" if busy else "—"])
            d.table(["#", "آدرس", "تسک فعال"], rows)
        else:
            d.p(f"{e('info')} هنوز ولتی ثبت نکرده‌اید. با دکمهٔ «افزودن ولت» شروع کنید.")
        d.note(f"فقط <b>ولت جداگانه (burner)</b> با کمی {lt('ETH')} برای گس — <b>هرگز ولت اصلی</b>.", "shield")
        rows = [[btn(ui.short(w["address"]), copy=w["address"], icon="link"),
                 btn("حذف", f"wdq:{w['id']}", style="danger", icon="trash")] for w in ws]
        if len(ws) < 5:
            rows.append([btn("افزودن ولت", "wadd", style="success", icon="plus")])
        return d, kb(*rows)

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
        await self.show(q, Doc().h("حذف این ولت؟", "warn", 3).p(mono(w["address"]))
                        .p("کلید رمزنگاری‌شدهٔ آن از ربات پاک می‌شود. <b>دارایی ولت دست نمی‌خورد.</b>"),
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
            return await self.paywall(q.message, m)
        if len(self.db.wallets(q.from_user.id)) >= 5:
            return await self.alert(q, "حداکثر ۵ ولت مجاز است.", True)
        c.user_data["state"] = "await_pk"
        await self.say(q.message,
                       Doc().h("افزودن ولت burner", "lock", 3)
                       .p(f"کلید خصوصی ({lt('Private Key')}) ولت را بفرستید — {ltr('64')} کاراکتر {lt('hex')}، با یا بدون {lt('0x')}.")
                       .checklist([(True, "پیام شما <b>همان لحظه پاک</b> می‌شود"),
                                   (True, f"کلید با {lt('AES-256-GCM')} رمزنگاری و ذخیره می‌شود"),
                                   (True, "فقط برای امضای تراکنش مینت استفاده می‌شود")])
                       .note("<b>فقط ولت جداگانه با کمی ETH برای گس — نه ولت اصلی!</b>", "warn"),
                       kb([btn("انصراف", "xc", style="danger", icon="no")]))

    async def wallet_del(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        wid = int(q.data.split(":")[1])
        ok = self.db.delete_wallet(q.from_user.id, wid)
        self.db.audit(q.from_user.id, "wallet_delete", f"{wid} {ok}")
        if not ok:
            await self.alert(q, "این ولت تسک فعال دارد؛ اول تسک را لغو کنید.", True)
        d, mk = self.wallets_view(q.from_user.id)
        await self.show(q, (Doc().p(f"{e('ok')} <b>ولت حذف شد.</b>").extend(d) if ok else d), mk)

    async def got_pk(self, u: Update, c):
        uid = u.effective_user.id
        text = u.message.text or ""
        try:
            await u.message.delete()
        except Exception:  # noqa: BLE001
            await R.send(u.get_bot(), uid, Doc().p(f"{e('siren')} نتوانستم پیام را پاک کنم؛ <b>لطفاً همین الان خودتان آن را پاک کنید.</b>"))
        send = lambda d, mk=None: R.send(u.get_bot(), uid, d, mk)
        try:
            pk = normalize_private_key(text)
        except Exception:  # noqa: BLE001
            return await send(Doc().p(f"{e('no')} <b>کلید معتبر نیست.</b> باید {ltr('64')} کاراکتر {lt('hex')} باشد. دوباره بفرستید یا انصراف بزنید."),
                              kb([btn("انصراف", "xc", style="danger", icon="no")]))
        addr, blob = self.vault.encrypt(uid, pk)
        try:
            bal = await self.blocking(pf.check_wallet, addr)
        except pf.PreflightError as ex:
            return await send(Doc().p(f"{e('no')} {esc(ex)}"))
        self.db.add_wallet(uid, addr, blob)
        self.db.audit(uid, "wallet_add", addr)
        c.user_data.pop("state", None)
        d = (Doc().h("ولت ثبت شد", "ok", 3)
             .kv([("آدرس", mono(addr)), ("موجودی", R.eth(bal)), ("شبکه", lt("Robinhood Chain"))]))
        if bal < pf.WARN_BALANCE_WEI:
            d.note(f"موجودی برای گس کم است؛ حداقل <b>{lt('0.0001 ETH')}</b> روی {lt('Robinhood Chain')} واریز کنید.", "warn")
        d.footer(f"{e('lock')} کلید خصوصی پاک و رمزنگاری شد.")
        await send(d, kb([btn("کپی آدرس", copy=addr, icon="link"), btn("اکسپلورر", url=ui.addr_url(addr), icon="search")]))
        await send(Doc().p(f"{e('next')} حالا لینک کالکشن را بفرستید یا از منو ادامه دهید."), self.menu())

    # ------------------------------------------------------------------ OpenSea API key
    def key_menu(self, uid):
        ks = self.db.apikeys(uid)
        now = time.time()
        valid = sum(1 for k in ks if not (k["expires_at"] and k["expires_at"] < now))
        d = Doc().h(f"کلیدهای {lt('API')} شخصی {lt('OpenSea')}", "lock", 2)
        src = {"instant": "فوری", "manual": "دستی", "legacy": "قدیمی"}
        if ks:
            rows = []
            for k in ks:
                busy = self.db.q("SELECT COUNT(*) FROM tasks WHERE apikey_id=? AND status IN ('scheduled','running')", (k["id"],))[0][0]
                dead = k["expires_at"] and k["expires_at"] < now
                soon = not dead and k["expires_at"] and k["expires_at"] - now < 86400
                st = (f"{e('red')} منقضی" if dead else f"{e('warn')} رو به انقضا" if soon else f"{e('green')} معتبر")
                exp = "—" if not k["expires_at"] else (R.tm(k["expires_at"], "d") if dead else R.rel(k["expires_at"]))
                rows.append([lt(k["label"] or k["id"]), st, src.get(k["source"], esc(k["source"])), exp, ltr(busy) if busy else "—"])
            d.table(["کلید", "وضعیت", "نوع", "انقضا", "تسک"], rows, caption=f"{ltr(valid)} معتبر از {ltr(len(ks))}")
        else:
            d.p(f"{e('red')} <b>هنوز کلیدی ثبت نشده</b> — برای مینت حداقل یک کلید لازم است.")
        d.details(f"{e('idea')} چرا چند کلید؟ و چطور کلید بسازم؟",
                  f"<p>هر مرحله کلید اختصاصی خودش را می‌گیرد تا سهمیهٔ <b>۵ درخواست مینت</b> {lt('OpenSea')} بین مرحله‌های هم‌زمان تقسیم نشود.</p>"
                  f"<p>{e('warn')} کلیدهای یک حساب {lt('OpenSea')} سهمیهٔ مشترک دارند و کلید فوری روزانه ۲ عدد برای هر {lt('IP')} است؛ "
                  "کلیدها را از اینترنت یا حساب‌های مختلف بسازید.</p>"
                  "<ol><li><b>خودکار:</b> دکمهٔ «⚡ ساخت خودکار کلید API» در منو (از اینترنت خود شما).</li>"
                  f"<li><b>دستی:</b> <code>{ltr('curl -X POST https://api.opensea.io/api/v2/auth/keys')}</code> یا کلید کامل از "
                  f"{lt('opensea.io/settings/developer')} و ارسال همین‌جا.</li></ol>", open_=not ks)
        rows = [[btn(f"حذف کلید {k['label'] or k['id']}", f"kdq:{k['id']}", style="danger", icon="trash")] for k in ks]
        rows.append([btn("افزودن کلید (دستی)", "kset", style="success", icon="plus")])
        return d, kb(*rows)

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
        await self.show(q, Doc().h(f"حذف کلید {lt(k['label'] or k['id'])}؟", "warn", 3)
                        .p("تسک‌هایی که بعداً شروع می‌شوند از کلیدهای دیگر شما استفاده می‌کنند."),
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
        d, mk = self.key_menu(q.from_user.id)
        await self.show(q, (Doc().p(f"{e('ok')} <b>کلید حذف شد.</b>").extend(d) if ok else d), mk)

    async def key_set(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        c.user_data["state"] = "await_key"
        await self.say(q.message, Doc().h(f"افزودن کلید {lt('API')}", "lock", 3)
                       .p(f"کلید {lt('API')} یا <b>کل خروجی {lt('JSON')}</b> دستور {lt('curl')} را بفرستید.")
                       .footer(f"{e('shield')} پیام شما بعد از دریافت پاک می‌شود."),
                       kb([btn("انصراف", "xc", style="danger", icon="no")]))

    async def store_key(self, u: Update, c, key, expires_at, source):
        uid = u.effective_user.id
        send = lambda d, mk=None: R.send(u.get_bot(), uid, d, mk)
        try:
            ok, why = await self.blocking(self.os.test_key, key)
        except OpenSeaError as ex:
            return await send(Doc().p(f"{e('warn')} تست کلید ممکن نشد: {lt(ex)}").p("بعداً دوباره امتحان کنید."))
        if not ok:
            return await send(Doc().p(f"{e('no')} <b>کلید توسط {lt('OpenSea')} رد شد:</b> {lt(why)}"))
        import hashlib
        fp = hashlib.sha256(("osh-key|" + key).encode()).hexdigest()[:32]
        r = self.db.add_apikey(uid, self.vault.seal(uid, "apikey", key), expires_at, source, fp)
        c.user_data.pop("state", None)
        if r == "dup":
            return await send(Doc().p(f"{e('info')} این کلید قبلاً ثبت شده است."), self.menu())
        if r == "full":
            return await send(Doc().p(f"{e('no')} حداکثر {ltr(self.db.MAX_KEYS)} کلید مجاز است؛ یکی را حذف کنید."))
        self.db.audit(uid, "apikey_add", f"{r} {source}")
        n = len(self.db.apikeys(uid, valid_after=time.time()))
        await send(Doc().h(f"کلید {lt('API')} تست و ذخیره شد", "ok", 3)
                   .kv([("نوع", "فوری" if source == "instant" else "دستی"),
                        ("انقضا", R.when(expires_at) if expires_at else "نامشخص"), ("کلیدهای معتبر شما", ltr(n))]),
                   self.menu())

    @staticmethod
    def parse_key_text(text):
        """Bare key or OpenSea's JSON ({"api_key", "expires_at"} / {"errors": [...]}). Never raises (any shape)."""
        t = (text or "").strip()
        exp = None
        if t.startswith("{"):
            try:
                j = json.loads(t)
            except ValueError:
                return None, None
            if not isinstance(j, dict):
                return None, None
            err = j.get("errors")
            if err:
                first = err[0] if isinstance(err, list) else err
                return "ERR:" + str(first)[:150], None
            t = str(j.get("api_key") or "").strip()
            ea = j.get("expires_at")
            if isinstance(ea, (int, float)) and not isinstance(ea, bool) and ea > 0:
                exp = int(ea)
            elif isinstance(ea, str) and ea:
                try:
                    exp = int(dt.datetime.fromisoformat(ea.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    exp = None
        return t, exp

    async def got_key(self, u: Update, c):
        text = u.message.text
        try:                                                 # delete FIRST: the message holds a secret
            await u.message.delete()
        except Exception:  # noqa: BLE001
            pass
        key, exp = self.parse_key_text(text)
        if key and key.startswith("ERR:"):
            return await self.say(u, Doc().p(f"{e('no')} <b>{lt('OpenSea')} خطا داد:</b> {lt(key[4:])}")
                                  .footer(f"سقف ساخت کلید فوری ۲ عدد در روز برای هر {lt('IP')} است."))
        if not key:
            return await self.say(u, Doc().p(f"{e('no')} کلید پیدا نشد. دوباره بفرستید یا {lt('/cancel')}"))
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "manual")

    async def webapp_data(self, u: Update, c):
        data = u.effective_message.web_app_data.data
        key, exp = self.parse_key_text(data)
        if key and key.startswith("ERR:"):
            return await self.say(u, Doc().p(f"{e('no')} <b>{lt('OpenSea')} خطا داد:</b> {lt(key[4:])}")
                                  .footer(f"سقف ساخت کلید فوری ۲ عدد در روز برای هر {lt('IP')} است."))
        if not key:
            return await self.say(u, Doc().p(f"{e('no')} داده نامعتبر از صفحهٔ ساخت کلید"))
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "instant")

    # ------------------------------------------------------------------ tasks
    async def new_task(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        if (m := self.need_sub(uid)):
            return await self.paywall(u, m)
        if not self.db.apikey(uid):
            return await self.say(u, Doc().p(f"{e('lock')} اول کلید {lt('API')} شخصی {lt('OpenSea')} را ثبت کنید."),
                                  kb([btn("افزودن کلید API", "kset", style="success", icon="plus")]))
        if not self.db.wallets(uid):
            return await self.say(u, Doc().p(f"{e('coin')} اول یک ولت burner ثبت کنید."),
                                  kb([btn("افزودن ولت", "wadd", style="success", icon="plus")]))
        if self.db.active_count(uid) >= self.max_active():
            return await self.say(u, Doc().p(f"{e('stop')} حداکثر {ltr(self.max_active())} تسک فعال مجاز است؛ صبر کنید یکی تمام شود یا لغوش کنید."))
        c.user_data["state"] = "await_link"
        await self.say(u, Doc().h("تسک جدید", "link", 3)
                       .p(f"لینک کالکشن {lt('OpenSea')} یا فقط {lt('slug')} را بفرستید.")
                       .table(["نمونهٔ قابل‌قبول"], [[mono("https://opensea.io/collection/aerent")],
                                                    [mono("https://opensea.io/item/robinhood/0x…/1")], [mono("aerent")]])
                       .footer("لینک داخل یک متن بلندتر یا پیام فورواردی هم پیدا می‌شود."),
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
            return await self.say(u, Doc().p(f"{e('warn')} <b>این پیام لینک کالکشن نیست.</b>")
                                  .p(f"فقط لینک را بفرستید، مثل {mono('https://opensea.io/collection/aerent')} یا فقط {lt('slug')} مثل {lt('aerent')}."),
                                  kb([btn("انصراف", "xc", style="danger", icon="no")]))
        await u.effective_chat.send_action("typing")
        msg = await self.say(u, Doc().p(f"{e('search')} <b>در حال بررسی دراپ</b> روی {lt('OpenSea')} و زنجیره…"))
        try:
            key = self.user_key(uid)
            slug = await self.blocking(self.os.resolve_slug, link, key)
            drop = await self.blocking(self.os.drop, slug, key)
            nft = pf.check_drop(drop)
            await self.blocking(pf.check_chain_contract, nft)
        except (OpenSeaError, pf.PreflightError, VaultError) as ex:
            return await self.screen(msg, Doc().h("بررسی دراپ ناموفق بود", "no", 3).p(esc(ex)),
                                     kb([btn("انصراف", "xc", style="danger", icon="no")]))
        stages = pf.pick_stages(drop)
        c.user_data.pop("state", None)
        if not stages:
            return await self.screen(msg, Doc().p(f"{e('wait')} همهٔ مرحله‌های این دراپ تمام شده‌اند."))
        ok_st, rows, notes = [], [], []
        for s in stages:
            why = pf.unsupported_reason(s)
            lim = pf.stage_wallet_limit(s)
            rows.append([f"{e('green') if not why else e('stop')} <b>{lt(s['label'])}</b>",
                         self.kind_fa(s["kind"], s["type"]), R.price(s["price_wei"]),
                         ltr(lim) if lim else "؟", f"{R.tm(s['start'])}<br>{R.rel(s['start'])}"])
            if why:
                notes.append(f"<b>{lt(s['label'])}:</b> {esc(why)}")
            else:
                ok_st.append(s)
        try:
            tot, mx = int(drop.get("total_supply") or 0), int(drop.get("max_supply") or 0)
        except (TypeError, ValueError):
            tot = mx = 0
        name = drop.get("collection_name") or slug
        d = (Doc().h(lt(name), "gem", 2)
             .kv([("قرارداد", mono(nft)), ("شبکه", lt("Robinhood Chain")),
                  ("عرضه", f"{ltr(f'{tot:,} / {mx:,}')}  {ltr(ui.bar(tot, mx))}" if mx else lt(f"{drop.get('total_supply')}/{drop.get('max_supply')}"))])
             .table(["مرحله", "نوع", "قیمت", "سقف ولت", "شروع"], rows, caption="مرحله‌ها"))
        if notes:
            d.quote("\n".join(notes))
        if not ok_st:
            return await self.screen(msg, d.p(f"{e('no')} <b>هیچ مرحلهٔ قابل‌مینتی ندارد.</b>"))
        c.user_data["plan"] = {"step": "wallet", "slug": slug, "nft": nft, "name": name, "stages": ok_st}
        d.hr().p(f"{e('coin')} <b>با کدام ولت مینت شود؟</b>").footer(
            f"ربات برای همان ولت {lt('allowlist')} و سقف هر مرحله را از {lt('OpenSea')} چک می‌کند.")
        await self.screen(msg, d, self.wallet_pick_kb(uid))

    def wallet_pick_kb(self, uid):
        rows = [[btn(f"{ui.short(w['address'])}", f"pw:{w['id']}", icon="coin")] for w in self.db.wallets(uid)]
        rows.append([btn("انصراف", "px", style="danger", icon="no")])
        return kb(*rows)

    @staticmethod
    def kind_fa(kind, typ=None):
        return {"signed": f"{ltr('Allowlist')} (امضادار)", "public": f"{ltr('Public')} (عمومی)"}.get(kind, lt(typ or "?"))

    @staticmethod
    def price_fa(p):
        return R.price(p)

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
            return await self.say(q.message, Doc().p(f"{e('wait')} {EXPIRED}"))
        nft, slug = p["nft"], p["slug"]
        if p.get("step") != "wallet":                                # double tap on a wallet button
            return
        p["step"] = "checking"
        chk = lambda a, b, c_: (Doc().h(f"پیش‌بررسی ولت {mono(ui.short(w['address']))}", "search", 3)
                                .checklist([(a, "موجودی و مینت‌های قبلی روی زنجیره"),
                                            (b, f"ورود به {lt('OpenSea')} با ولت ({lt('SIWE')})"),
                                            (c_, f"{lt('allowlist')} و سقف هر مرحله")])
                                .footer("چند ثانیه صبر کنید…"))
        m = await self.show(q, chk(False, False, False))
        try:
            bal = await self.blocking(pf.check_wallet, w["address"])
            done = await self.blocking(pf.minted_count, nft, w["address"])
        except pf.PreflightError as ex:
            p["step"] = "wallet"                                     # let the user retry / pick another wallet
            return await self.screen(m, Doc().p(f"{e('no')} {esc(ex)}").p(f"{e('coin')} <b>با کدام ولت مینت شود؟</b>"),
                                     self.wallet_pick_kb(uid))
        m = await self.screen(m, chk(True, False, False))
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
            m = await self.screen(m, chk(True, True, False))
            el = await self.blocking(self.os.eligibility_full, slug, self.user_key(uid), jwt)
        except (OpenSeaError, VaultError) as ex:
            el_err = str(ex)[:80]
        for s in p["stages"]:
            s["limit"] = pf.stage_wallet_limit(s)                    # per-stage limit as shown by OpenSea
            s["note"] = None
            if s["kind"] == "public" and pub and s["price_wei"] > 0 and pub[0] != s["price_wei"]:
                s["note"] = f"قیمت روی زنجیره ({pub[0] / 1e18:.6f}) با OpenSea فرق دارد"
            el_s = el.get(norm_uuid(s["uuid"])) or {}
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
        d = (Doc().h("انتخاب مرحله‌ها و تعداد", "list", 3)
             .kv([("ولت", mono(p["addr"])), ("موجودی", R.eth(p["bal"])), ("مینت‌های قبلی در این کالکشن", ltr(p["done"]))]))
        rows = []
        for s in p["stages"]:
            elig = {True: f"{e('ok')} هست", False: f"{e('no')} نیست"}.get(s["eligible"], f"{e('warn')} نامعلوم")
            if s["kind"] == "public":
                elig = f"{e('green')} عمومی" if s["eligible"] is not False else elig
            mark = e("ok") if s["qty"] else (e("stop") if s["why_no"] else e("timer"))
            room = (f"<i>{esc(s['why_no'])}</i>" if s["why_no"] else f"حداکثر <b>{ltr(s['maxq'])}</b>")
            rows.append([f"{mark} <b>{lt(s['label'])}</b><br>{R.tm(s['start'])}", elig,
                         lt(f"Limit {s['limit'] if s['limit'] else '?'}"), room,
                         f"<b>{ltr(s['qty'])}</b>" if s["qty"] else "—"])
        d.table(["مرحله", lt("allowlist"), "سقف", "قابل مینت", "انتخاب"], rows)
        if p.get("el_err"):
            d.note(f"چک {lt('allowlist')} ممکن نشد ({lt(p['el_err'])})؛ در زمان مینت دوباره چک می‌شود.", "warn")
        d.quote(f"{e('info')} سقف هر ولت <b>جدا برای هر مرحله</b> است (طبق {lt('OpenSea')}): مثلاً {lt('Limit 4')} در "
                f"مرحلهٔ اول و {lt('Limit 1')} در مرحلهٔ دوم یعنی جمعاً ۵ عدد.")
        d.p(f"{e('next')} مرحله(ها) را انتخاب و تعداد هرکدام را مشخص کنید، بعد «ادامه».")
        return d

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
        if new:
            await self.say(msg, self.select_text(p), kb(*rows))
        else:
            await self.screen(msg, self.select_text(p), kb(*rows))

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
            return await self.say(q.message, Doc().p(f"{e('wait')} {EXPIRED}"))
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
        d = (Doc().h(lt(s["label"]), "pin", 3)
             .kv([("سقف هر ولت در این مرحله", lt(f"Limit {lim or '?'} per wallet")),
                  ("حداکثر قابل مینت برای شما در این مرحله", f"<b>{ltr(s['maxq'])}</b>"),
                  ("قیمت هر عدد", R.price(s["price_wei"])), ("شروع", R.when(s["start"]))])
             .p(f"{e('next')} <b>می‌خواهید در این مرحله چند عدد مینت کنید؟</b>"))
        await self.show(q, d, kb(*rows))

    async def plan_qty(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        p = c.user_data.get("plan")
        try:
            _, i, v = q.data.split(":"); i = int(i); s = p["stages"][i]
            assert p["step"] == "qty" and p.get("qi") == i
        except (TypeError, KeyError, IndexError, ValueError, AssertionError):
            return await self.say(q.message, Doc().p(f"{e('wait')} {EXPIRED}"))
        if v == "c":
            c.user_data["state"] = "await_qty"
            p["qmsg"] = q.message.message_id
            return await self.say(q.message, Doc().p(f"{e('edit')} عدد مورد نظر را بفرستید — از {ltr(1)} تا {ltr(s['maxq'])}."),
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
            return await self.say(u, Doc().p(f"{e('wait')} {EXPIRED}"))
        s = p["stages"][p["qi"]]
        if not t.isdigit() or not 1 <= int(t) <= s["maxq"]:
            return await self.say(u, Doc().p(f"{e('no')} یک عدد بین {ltr(1)} و {ltr(s['maxq'])} بفرستید (یا {lt('/cancel')})."))
        s["qty"] = int(t); p["step"] = "select"; p.pop("qi", None)
        c.user_data.pop("state", None)
        await self.render_select(u.message, p, new=True)

    async def plan_back(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        p = c.user_data.get("plan")
        if not p or "wid" not in p:
            return await self.say(q.message, Doc().p(f"{e('wait')} {EXPIRED}"))
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
            return await self.say(q.message, Doc().p(f"{e('wait')} {EXPIRED}"))
        proj = self.project(p)
        pref = self.db.prefs(uid)
        for s, _ in proj:
            s["min_unit"], s["max_unit"] = self.unit_bounds(s["price_wei"], pref["price_policy"], pref["max_increase_pct"])
        total = sum(s["max_unit"] * s["qty"] for s, _ in proj)
        gas = pf.WARN_BALANCE_WEI * len(proj)
        ok = True
        d = (Doc().h("خلاصهٔ برنامهٔ مینت", "list", 2)
             .kv([("کالکشن", lt(p["name"])), ("ولت", mono(p["addr"]))]))
        rows = []
        for n, (s, eff) in enumerate(proj, 1):
            pr, qt = s["price_wei"], s["qty"]
            rows.append([ltr(n), f"<b>{lt(s['label'])}</b><br>{self.kind_fa(s['kind'])}",
                         f"{R.tm(s['start'])}<br>{R.rel(s['start'])}", f"<b>{ltr(qt)}</b>",
                         "رایگان" if not pr else ltr(f"{pr / 1e18:.6f} × {qt} = {pr * qt / 1e18:.6f} ETH")])
        d.table(["#", "مرحله", "شروع", "تعداد", "هزینه"], rows, caption="مرحله‌های انتخاب‌شده")
        ranges = [(s, s["min_unit"], s["max_unit"]) for s, _ in proj if s["max_unit"] != s["price_wei"] or s["min_unit"] != s["price_wei"]]
        money = [("جمع قیمت (حداکثر)", f"<b>{R.eth(total)}</b>"), ("گس تقریبی", ltr(f"~{gas / 1e18:.4f} ETH")),
                 ("موجودی ولت", R.eth(p["bal"]))]
        money += [(f"قیمت قابل‌قبول {lt(s['label'][:16])}", ltr(f"{lo / 1e18:.6f} – {hi / 1e18:.6f} ETH")) for s, lo, hi in ranges]
        d.kv(money, caption="هزینه")
        if total:
            terms = [f"{R.num(s['max_unit'])} \\times {s['qty']}" for s, _ in proj if s["max_unit"]]
            d.math(" + ".join(terms) + f" = {R.num(total)}\\ \\mathrm{{ETH}}", "")
        if p["bal"] < total + gas:
            d.note("موجودی برای همهٔ برنامه کافی نیست؛ ربات در هر مرحله فقط به اندازهٔ موجودی همان لحظه مینت می‌کند.", "warn")
        last = max(s["start"] for s, _ in proj)
        checks = [(True, f"<b>اگر قیمت عوض شد:</b> {esc(self.policy_line(pref))} <i>(قابل تغییر در «⚙️ تنظیمات»)</i>")]
        assign = self.db.pick_keys(uid, [{"start": s["start"], "end": s["end"]} for s, _ in proj])
        if any(kid is None for kid, _ in assign):
            ok = False
            checks.append((False, f"برای بعضی مرحله‌ها هیچ کلید {lt('API')} معتبری تا زمان شروع ندارید؛ کلید جدید ثبت کنید."))
        else:
            labels = {k["id"]: k["label"] or str(k["id"]) for k in self.db.apikeys(uid)}
            shared = sum(1 for _, sh in assign if sh)
            checks.append((not shared, "<b>کلید هر مرحله:</b> " + "، ".join(f"{lt(s['label'][:16])} ← {lt(labels.get(kid, kid))}"
                                                                          for (s, _), (kid, _) in zip(proj, assign))
                           + (f" — {ltr(shared)} مرحله کلید مشترک دارد (سهمیهٔ ۵ درخواست {lt('OpenSea')} تقسیم می‌شود)." if shared else "")))
        usr = self.db.user(uid)
        if usr and usr["sub_until"] < last:
            ok = False
            checks.append((False, f"اشتراک شما قبل از شروع آخرین مرحله تمام می‌شود ({R.tm(usr['sub_until'])})؛ اول تمدید کنید."))
        else:
            checks.append((True, "اشتراک تا شروع آخرین مرحله معتبر است"))
        if self.db.active_count(uid) + len(proj) > self.max_active():
            ok = False
            checks.append((False, f"با این برنامه از سقف {ltr(self.max_active())} تسک فعال رد می‌شوید."))
        d.h("بررسی نهایی", "shield", 4).checklist(checks)
        d.quote(f"{e('shield')} ربات هرگز بیشتر از «قیمت قابل‌قبول × تعداد» نمی‌پردازد و همیشه همان مبلغی را "
                "می‌فرستد که قرارداد می‌خواهد. تعداد فقط ممکن است کمتر شود (سقف ولت / عرضه / موجودی)، هرگز بیشتر.")
        d.p(f"{e('next')} <b>تأیید می‌کنید؟</b>" if ok else f"{e('stop')} <b>برنامه قابل ثبت نیست.</b>")
        p["step"] = "summary"
        rows = ([[btn("تأیید و مسلح کردن", "pa", style="success", icon="bolt")]] if ok else []) + \
               [[btn("ویرایش", "pb", icon="edit"), btn("انصراف", "px", style="danger", icon="no")]]
        await self.show(q, d, kb(*rows))

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
            return await self.paywall(q.message, m)
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
        try:
            res = self.db.add_plan(uid, p["wid"], p["slug"], p["nft"], items, self.max_active(), plan_id)
        except Exception:                                    # never leave the plan stuck in "arming"
            p["step"] = "summary"
            raise
        if res == "nowallet":
            c.user_data.pop("plan", None)
            return await self.say(q.message, Doc().p(f"{e('stop')} این ولت حذف شده است؛ تسک جدید بسازید."), self.menu())
        if res in ("dup", "limit"):
            p["step"] = "summary"
            return await self.say(q.message, Doc().p(f"{e('stop')} " + ("برای یکی از این مرحله‌ها با این ولت قبلاً تسک فعال دارید."
                                                                        if res == "dup" else "سقف تسک فعال پر است.")))
        self.db.audit(uid, "plan_add", f"{plan_id} {p['slug']} " + ", ".join(f"{t}:{s['label']}x{s['qty']}" for t, s in zip(res, sel)))
        c.user_data.pop("plan", None)
        rows = []
        for t, s in zip(res, sel):
            prep = "همین الان آماده می‌شود" if s["start"] <= time.time() + self.cfg.spawn_before_s else \
                f"{self.cfg.spawn_before_s // 60} دقیقه قبل از شروع"
            rows.append([f"<b>{ltr(f'#{t}')}</b>", lt(s["label"]), ltr(s["qty"]), f"{R.tm(s['start'])}<br>{R.rel(s['start'])}", prep])
        try:                                                     # the summary screen becomes a read-only record
            await q.message.edit_reply_markup(kb([btn("ثبت شد", disabled=True, style="success", icon="ok")]))
        except Exception:  # noqa: BLE001
            pass
        d = (Doc().h("مسلح شد!", "fire", 2)
             .p("ربات منتظر لحظهٔ شروع هر مرحله می‌ماند و <b>همان لحظه شلیک می‌کند</b>.")
             .table(["تسک", "مرحله", "تعداد", "شروع", "آماده‌سازی"], rows)
             .footer(f"{e('bell')} نتیجهٔ هر مرحله همین‌جا اطلاع داده می‌شود · «📋 تسک‌ها» برای پیگیری"))
        await self.say(q.message, d, self.menu(), effect=ui.EFFECT_FIRE)

    async def plan_cancel(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        c.user_data.pop("plan", None); c.user_data.pop("state", None)
        try:
            await q.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        await self.say(q.message, Doc().p(f"{e('stop')} <b>لغو شد.</b>"), self.menu())

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
        await self.say(q.message, Doc().p(f"{e('stop')} <b>لغو شد.</b>"), self.menu())

    async def stale(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        await self.say(q.message, Doc().p(f"{e('info')} این دکمه مربوط به نسخهٔ قبلی است؛ دوباره «🎯 تسک جدید» را بزنید."))

    # ------------------------------------------------------------------ settings
    def settings_view(self, uid):
        pref = self.db.prefs(uid)
        cur = pref["price_policy"]
        pct = pref["max_increase_pct"]
        rows_ = [(k, f"{e('ok') if cur == k else e('timer')} <b>{esc(POLICY_SHORT[k])}</b>", desc) for k, desc in (
            ("exact", "هر تغییری ← هیچ تراکنشی ارسال نمی‌شود (امن‌ترین)"),
            ("lower_ok", "ارزان‌تر شد ← مینت · گران‌تر شد ← بیخیال"),
            ("cap", f"تا {ltr(f'+{pct}%') if cur == 'cap' else 'درصد تعیین‌شده'} گران‌تر هم مینت · ارزان‌تر ← بیخیال"),
            ("any_up", f"ارزان‌تر یا گران‌تر تا {ltr(f'+{pct}%') if cur == 'any_up' else 'سقف'} ← مینت (ریسک بالاتر)"))]
        txt = (Doc().h("تنظیمات", "gear", 2)
               .h("واکنش به تغییر قیمت", "usd", 4)
               .p("اگر سازنده هنگام شروع یا حین مینت قیمت را عوض کند، ربات چه کند؟")
               .table(["حالت", "رفتار"], [[a, b] for _, a, b in rows_])
               .kv([("فعلی", f"<b>{esc(self.policy_line(pref))}</b>"), ("سقف تسک فعال", ltr(self.max_active()))])
               .note("ربات همیشه دقیقاً همان مبلغی را می‌فرستد که قرارداد می‌خواهد و هرگز بیشتر از سقف. "
                     "تنظیم روی برنامه‌هایی اعمال می‌شود که از این به بعد مسلح می‌کنید.", "shield"))
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
            return (Doc().h("تسک‌های شما", "list", 2)
                    .p(f"{e('info')} هنوز تسکی ندارید. لینک کالکشن را بفرستید یا «🎯 تسک جدید» را بزنید."),
                    kb([btn("تسک جدید", "nt", style="primary", icon="bolt")]))
        act = [t for t in ts if t["status"] in ("scheduled", "running")]
        done = [t for t in ts if t["status"] not in ("scheduled", "running")][:8]
        minted = sum(1 for t in ts if t["status"] == "minted")
        d = Doc().h("تسک‌های شما", "list", 2).kv([("فعال", ltr(len(act))), ("موفق (۲۰ تسک اخیر)", ltr(minted))])
        rows = []
        for group, head in ((act, "فعال"), (done, "اخیر")):
            if not group:
                continue
            trs = []
            for t in group:
                ic, lab = STATUS.get(t["status"], ("info", t["status"]))
                trs.append(["<b>" + ltr("#" + str(t["id"])) + "</b>",
                            f"{lt(t['slug'])}<br>{lt(t['stage_label'])} × {ltr(t['quantity'])}",
                            f"{e(ic)} {lab}", f"{R.tm(t['start_ts'], 'd')}<br>{R.rel(t['start_ts'])}"])
                row = [btn(f"#{t['id']} جزئیات", f"td:{t['id']}", icon="search")]
                if t["status"] in ("scheduled", "running"):
                    row.append(btn(f"لغو #{t['id']}", f"tcq:{t['id']}", style="danger", icon="stop"))
                elif t["tx_hash"]:
                    row.append(btn("تراکنش", url=ui.tx_url(t["tx_hash"]), icon="link"))
                rows.append(row)
            d.table(["#", "کالکشن / مرحله", "وضعیت", "شروع"], trs, caption=head)
        rows.append([btn("بروزرسانی", "tl", icon="refresh")])
        return d, kb(*rows)

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
        d = (Doc().h("تسک " + ltr("#" + str(t["id"])) + f" — {lab}", ic, 3)
             .kv([("کالکشن", lt(t["slug"])), ("مرحله", f"{lt(t['stage_label'])} · {self.kind_fa(t['kind'])}"),
                  ("تعداد", ltr(t["quantity"])), ("شروع", R.when(t["start_ts"])),
                  ("ولت", mono(w["address"])) if w else None,
                  ("تراکنش", mono(t["tx_hash"][:10] + "…" + t["tx_hash"][-6:])) if t["tx_hash"] else None]))
        if t["result"]:
            d.details("نتیجه", f"<p>{esc(t['result']).replace(chr(10), '<br>')}</p>", open_=True)
        rows = []
        if t["tx_hash"]:
            rows.append([btn("مشاهده در اکسپلورر", url=ui.tx_url(t["tx_hash"]), icon="search"),
                         btn("کپی tx", copy=t["tx_hash"], icon="link")])
        if t["status"] in ("scheduled", "running"):
            rows.append([btn("لغو این تسک", f"tcq:{t['id']}", style="danger", icon="stop")])
        rows.append([btn("برگشت به لیست", "tl", icon="next")])
        await self.show(q, d, kb(*rows))

    async def task_cancel_ask(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        t = self.db.task(int(q.data.split(":")[1]))
        if not t or t["user_id"] != q.from_user.id or t["status"] not in ("scheduled", "running"):
            await self.alert(q, "این تسک دیگر قابل لغو نیست.")
            return await self.show(q, *self.tasks_view(q.from_user.id))
        await self.show(q, Doc().h("لغو تسک " + ltr("#" + str(t["id"])) + "؟", "warn", 3)
                        .kv([("کالکشن", lt(t["slug"])), ("مرحله", f"{lt(t['stage_label'])} × {ltr(t['quantity'])}"),
                             ("شروع", R.when(t["start_ts"]))])
                        .p("بعد از لغو، ربات برای این مرحله <b>شلیک نمی‌کند</b>."),
                        kb([btn("بله، لغو کن", f"tc:{t['id']}", style="danger", icon="stop"),
                            btn("نه، برگرد", "tl", icon="next")]))

    async def task_cancel(self, u: Update, c):
        q = u.callback_query
        await self.safe_answer(q)
        tid = int(q.data.split(":")[1])
        ok = await self.blocking(self.runner.cancel, q.from_user.id, tid)
        d, mk = self.tasks_view(q.from_user.id)
        head = Doc().p(f"{e('ok')} <b>تسک {ltr(f'#{tid}')} لغو شد.</b>" if ok else f"{e('info')} تسک {ltr(f'#{tid}')} قابل لغو نیست.")
        await self.show(q, head.extend(d), mk)

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
            return await self.say(u, Doc().p(f"{e('info')} {hint} (یا {lt('/cancel')})."))
        # a pasted private key outside the flow: delete it anyway
        if re.fullmatch(r"(0[xX])?[0-9a-fA-F]{64}", t.strip().strip('"').strip("'")):
            try:
                await u.message.delete()
            except Exception:  # noqa: BLE001
                pass
            return await self.say(u, Doc().p(f"{e('siren')} پیام شبیه کلید خصوصی بود و <b>پاک شد</b>. برای ثبت ولت از «👛 ولت‌ها» اقدام کنید."))
        # A pasted OpenSea link (or forwarded text with one) starts a new task directly
        if self.extract_link(t) and ("opensea.io" in t.lower()):
            uid = u.effective_user.id
            self.db.ensure_user(uid, u.effective_user.username)
            if not self.need_sub(uid) and self.db.apikey(uid) and self.db.wallets(uid) \
                    and self.db.active_count(uid) < self.max_active():
                return await self.got_link(u, c)
            return await self.new_task(u, c)
        await self.say(u, Doc().p(f"{e('info')} از دکمه‌های منو استفاده کنید یا لینک کالکشن {lt('OpenSea')} را بفرستید."), self.menu())

    async def edited(self, u: Update, c):
        """An edited message is never treated as input. Only safety: if the NEW text looks like a private key,
        delete it (the original was handled when it was sent)."""
        m = u.edited_message
        t = (m.text or "").strip().strip('"').strip("'")
        if re.fullmatch(r"(0[xX])?[0-9a-fA-F]{64}", t):
            try:
                await m.delete()
            except Exception:  # noqa: BLE001
                return await self.say(m, Doc().p(f"{e('siren')} پیام ویرایش‌شده شبیه کلید خصوصی است؛ <b>لطفاً خودتان پاکش کنید.</b>"))
            await self.say(m, Doc().p(f"{e('siren')} پیام ویرایش‌شده شبیه کلید خصوصی بود و <b>پاک شد</b>. "
                                      "برای ثبت ولت از «👛 ولت‌ها» اقدام کنید."))

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

    async def settonprice(self, u: Update, c):
        """/settonprice <TON>   (0 = disable TON payments). Affects new invoices only."""
        if not self.is_admin(u):
            return
        try:
            v = c.args[0]
            nano = 0 if v.strip() in ("0", "off") else TP.parse_ton(v)
        except (IndexError, ValueError):
            return await self.say(u, "<code>/settonprice &lt;TON&gt;</code>  e.g. <code>/settonprice 5</code> · <code>0</code> = off")
        self.db.set_setting("ton_price_nano", nano)
        self.db.audit(u.effective_user.id, "admin_ton_price", str(nano))
        await self.say(u, f"{e('ok')} TON price = {TP.fmt_ton(nano) if nano else 'OFF'}"
                          f"{'' if self.ton.raw else ' (warning: TON_ADDRESS/TONAPI_KEY not configured)'}")

    async def tonassign(self, u: Update, c):
        """/tonassign <tx_hash> <user_id> [days] — credit an UNMATCHED incoming TON payment by hand."""
        if not self.is_admin(u):
            return
        try:
            tx, uid = c.args[0].lower(), int(c.args[1])
            days = float(c.args[2]) if len(c.args) > 2 else self.cfg.sub_days
            assert re.fullmatch(r"[0-9a-f]{64}", tx) and 0 < days <= 3650
        except (IndexError, ValueError, AssertionError):
            return await self.say(u, "<code>/tonassign &lt;tx_hash&gt; &lt;user_id&gt; [days]</code>")
        until = self.db.ton_assign(tx, uid, days, int(time.time()), u.effective_user.id)
        if until is None:
            return await self.say(u, f"{e('no')} not an unmatched TON payment (unknown or already credited)")
        self.notify(uid, Doc().h("پرداخت TON شما ثبت شد", "party", 3).kv([("اشتراک تا", R.when(until))]))
        await self.say(u, f"{e('ok')} credited to {uid}, until {fmt_ts(until)}")

    TON_RECEIVED_SQL = "SELECT COALESCE(SUM(value_nano),0) FROM ton_payments WHERE kind IN ('paid','partial','manual')"

    async def tonstatus(self, u: Update, c):
        """Admin dashboard: gateway health, price, today / 7 d / total revenue, open invoices, recent + unmatched payments."""
        if not self.is_admin(u):
            return
        q = lambda s_, a=(): self.db.q(s_, a)[0][0]
        now = int(time.time())
        rev = lambda since: TP.fmt_ton(q("SELECT COALESCE(SUM(value_nano),0) FROM ton_payments "
                                          "WHERE kind IN ('paid','partial','manual') AND utime>=?", (since,)))
        price = self.ton.price_nano()
        api = {True: f"{e('green')} وصل", False: f"{e('red')} قطع", None: f"{e('wait')} هنوز بررسی نشده"}[self.ton.healthy]
        d = (Doc().h(f"داشبورد {lt('TON')}", "chart", 2)
             .kv([("وضعیت", f"{e('green')} فعال" if self.ton.enabled() else f"{e('red')} غیرفعال"),
                  ("TonAPI", api), ("قیمت", f"{ltr(TP.fmt_ton(price))} {lt('TON')}" if price else "تنظیم نشده"),
                  ("آدرس", mono(self.ton.address or "-"))])
             .table(["امروز", "۷ روز", "کل"], [[ltr(rev(now - 86400)), ltr(rev(now - 7 * 86400)), ltr(TP.fmt_ton(q(self.TON_RECEIVED_SQL)))]],
                    caption=f"درآمد ({lt('TON')})")
             .kv([("فاکتور باز", ltr(q("SELECT COUNT(*) FROM ton_invoices WHERE status='open' AND expires>?", (now,)))),
                  ("فاکتور پرداخت‌شده", ltr(q("SELECT COUNT(*) FROM ton_invoices WHERE status='paid'"))),
                  ("بدون فاکتور", ltr(q("SELECT COUNT(*) FROM ton_payments WHERE kind='unmatched'")))]))
        recent = self.db.q("SELECT * FROM ton_payments WHERE kind IN ('paid','partial','manual','unmatched') ORDER BY lt DESC LIMIT 8")
        if recent:
            k = {"paid": e("ok"), "partial": e("wait"), "manual": e("edit"), "unmatched": e("siren")}
            d.table(["نوع", "مبلغ", "کاربر", "زمان"],
                    [[k[r["kind"]], ltr(TP.fmt_ton(r["value_nano"])), mono(r["user_id"]) if r["user_id"] else "—",
                      R.tm(r["utime"], "d")] for r in recent], caption="آخرین واریزها")
        un = self.db.ton_unmatched(5)
        if un:
            d.details(f"{e('siren')} واریزهای بدون فاکتور",
                      "<ul>" + "".join(f"<li>{ltr(TP.fmt_ton(r['value_nano']) + ' TON')} — {mono(r['tx_hash'])}</li>" for r in un) + "</ul>"
                      + f"<p>{mono('/tonassign <tx> <user_id>')}</p>", open_=True)
        await self.say(u, d)

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
            f"stars {q('SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0')} | "
            f"ton {TP.fmt_ton(q(self.TON_RECEIVED_SQL))}")

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
        app.add_handler(CommandHandler("settonprice", self.settonprice, filters=P))
        app.add_handler(CommandHandler("tonassign", self.tonassign, filters=P))
        app.add_handler(CommandHandler("tonstatus", self.tonstatus, filters=P))
        app.add_handler(CallbackQueryHandler(self.ton_pay, pattern=r"^tonpay$"))
        app.add_handler(CallbackQueryHandler(self.ton_check, pattern=r"^tonchk:\d+$"))
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
        NEW = filters.UpdateType.MESSAGE                      # edited messages are NOT new input (were crashing: u.message None)
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA & P & NEW, self.webapp_data))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & P & NEW, self.text))
        app.add_handler(MessageHandler(filters.TEXT & P & filters.UpdateType.EDITED_MESSAGE, self.edited))
        app.add_error_handler(self._on_error)
        self.app = app
        return app

    async def _on_error(self, update, c):
        log.error("handler error: %s: %s", type(c.error).__name__, str(c.error)[:300], exc_info=c.error)
        try:
            if update and getattr(update, "effective_chat", None):
                await R.send(c.bot, update.effective_chat.id,
                             Doc().p(f"{e('warn')} <b>مشکلی پیش آمد</b> — لطفاً دوباره تلاش کنید.")
                             .footer(f"اگر تکرار شد {lt('/cancel')} بزنید و از اول شروع کنید."))
        except Exception:  # noqa: BLE001
            pass

    COMMANDS = [("start", "شروع و وضعیت حساب"), ("new", "تسک مینت جدید"), ("tasks", "تسک‌های من"),
                ("wallets", "ولت‌ها"), ("key", "کلیدهای API"), ("settings", "تنظیمات"), ("sub", "اشتراک"),
                ("help", "راهنما"), ("cancel", "لغو کار فعلی")]

    async def _post_init(self, app):
        self.loop = asyncio.get_running_loop()
        self._runner_task = asyncio.create_task(self.runner.loop())
        self._ton_task = asyncio.create_task(self.ton.loop()) if (self.ton.raw and self.cfg.ton_api_key) else None
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
        tasks = [t for t in (getattr(self, "_runner_task", None), getattr(self, "_ton_task", None)) if t]
        for t in tasks:
            t.cancel()
        # wait for the cancellations to finish so no pending inner awaitable is destroyed at exit
        await asyncio.gather(*tasks, return_exceptions=True)


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
