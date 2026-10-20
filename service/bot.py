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

from telegram import (InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, KeyboardButton, LabeledPrice,
                      ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from . import preflight as pf
from .config import Config
from .db import DB
from .opensea import OpenSea, OpenSeaError
from .runner import Runner
from .vault import Vault, VaultError, load_master_key, normalize_private_key

log = logging.getLogger("bot")

B_SUB, B_WAL, B_KEY, B_NEW, B_TASKS, B_HELP = "💳 اشتراک", "👛 ولت‌ها", "🔑 کلید API", "🎯 تسک جدید", "📋 تسک‌ها", "❓ راهنما"
PRIVATE = filters.ChatType.PRIVATE


def fmt_ts(ts):
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    rel = ts - time.time()
    r = (f"{rel / 3600:.1f} ساعت دیگر" if rel > 3600 else f"{max(rel, 0) / 60:.0f} دقیقه دیگر") if rel > 0 else "شروع شده"
    return f"{d:%Y-%m-%d %H:%M} UTC ({r})"


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
        self.runner = Runner(self.db, self.vault, cfg, self.notify, extra_runner_args, admin_notify=self.notify_admins)

    # ------------------------------------------------------------------ infra
    def notify(self, uid, text):
        """Thread-safe (runner runs in an executor thread)."""
        if self.loop and self.app:
            asyncio.run_coroutine_threadsafe(self._send(uid, text), self.loop)

    def notify_admins(self, text):
        for a in self.cfg.admin_ids:
            self.notify(a, text)

    async def _send(self, uid, text):
        try:
            await self.app.bot.send_message(uid, text[:4000])
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s failed: %s", uid, type(e).__name__)

    def menu(self):
        rows = [[B_NEW, B_TASKS], [B_WAL, B_KEY], [B_SUB, B_HELP]]
        if self.cfg.keypage_url:
            rows.insert(2, [KeyboardButton("⚡ ساخت خودکار کلید API", web_app=WebAppInfo(self.cfg.keypage_url))])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    async def blocking(self, f, *a):
        return await asyncio.get_running_loop().run_in_executor(None, f, *a)

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
        await u.message.reply_text(
            "سلام 👋\nاین ربات مینت دراپ‌های OpenSea روی Robinhood Chain (Allowlist و Public، چند مرحله با تعداد دلخواه) "
            "را با سرعت بالا و خودکار برای ولت شما انجام می‌دهد.\n\n"
            "مراحل: ۱) اشتراک ۲) ثبت ولت ۳) ثبت کلید API شخصی OpenSea ۴) ارسال لینک کالکشن.\n"
            "از «❓ راهنما» توضیحات کامل و نکات امنیتی را بخوانید.", reply_markup=self.menu())

    async def help(self, u: Update, c):
        await u.message.reply_text(
            "📘 راهنما\n\n"
            "• دراپ‌های SeaDrop روی Robinhood Chain: مراحل امضادار (Allowlist) و عمومی (Public)، رایگان یا پولی.\n"
            "• یک لینک ← یک ولت ← چند مرحله با تعداد دلخواه برای هرکدام. ربات allowlist و سقف هر ولت را برای "
            "هر مرحله چک می‌کند (ورود SIWE به OpenSea) و در لحظهٔ شروع هر مرحله شلیک می‌کند.\n"
            "• سقف هر ولت در SeaDrop تجمعی است؛ ربات هرگز بیشتر از سقف، عرضه یا قیمت تأییدشده ارسال نمی‌کند.\n"
            "• هر کاربر با کلید API شخصی خودش کار می‌کند (سهمیه OpenSea بین کاربران تقسیم نمی‌شود).\n"
            "• کلیدهای فوری OpenSea ۷ روزه‌اند؛ قبل از هر دراپ مطمئن شوید کلید معتبر است.\n\n"
            "🔐 امنیت:\n"
            "• حتماً یک ولت جداگانه (burner) فقط با کمی ETH برای گس بسازید و NFT را بعد از مینت منتقل کنید.\n"
            "• کلید خصوصی پس از دریافت فوراً از چت پاک و رمزنگاری‌شده (AES-256-GCM) ذخیره می‌شود.\n"
            "• هیچ‌وقت کلید ولت اصلی خود را وارد نکنید.\n\n"
            "دستورات: /start /cancel /wallets /key /tasks /sub")

    async def cancel_cmd(self, u: Update, c):
        c.user_data.clear()
        await u.message.reply_text("لغو شد.", reply_markup=self.menu())

    # ------------------------------------------------------------------ subscription
    async def sub(self, u: Update, c):
        usr = self.db.ensure_user(u.effective_user.id, u.effective_user.username)
        if self.db.is_subscribed(usr["id"]):
            s = f"✅ اشتراک فعال تا {fmt_ts(usr['sub_until'])}"
        else:
            s = "❌ اشتراک فعال ندارید."
        await u.message.reply_text(s, reply_markup=IM([[IB(f"خرید/تمدید {self.cfg.sub_days} روزه — {self.cfg.price_stars} ⭐", callback_data="buy")]]))

    async def buy(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        await c.bot.send_invoice(uid, title=f"اشتراک {self.cfg.sub_days} روزه", description="اشتراک ربات مینت OpenSea-Hunter",
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
            await u.message.reply_text(f"✅ پرداخت موفق. اشتراک فعال تا {fmt_ts(new)}", reply_markup=self.menu())

    def need_sub(self, uid):
        return None if self.db.is_subscribed(uid) else "برای این بخش اشتراک فعال لازم است (💳 اشتراک)."

    # ------------------------------------------------------------------ wallets
    async def wallets(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        ws = self.db.wallets(uid)
        lines = [f"• <code>{w['address']}</code>" for w in ws] or ["(هنوز ولتی ثبت نکرده‌اید)"]
        kb = [[IB("➕ افزودن ولت", callback_data="wadd")]] + [[IB(f"🗑 حذف {w['address'][:8]}…", callback_data=f"wdel:{w['id']}")] for w in ws]
        await u.message.reply_text("👛 ولت‌های شما:\n" + "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    async def wallet_add(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        if (m := self.need_sub(q.from_user.id)):
            return await q.message.reply_text(m)
        if len(self.db.wallets(q.from_user.id)) >= 5:
            return await q.message.reply_text("حداکثر ۵ ولت مجاز است.")
        c.user_data["state"] = "await_pk"
        await q.message.reply_text(
            "🔐 کلید خصوصی (Private Key) ولت burner را ارسال کنید (۶۴ کاراکتر hex).\n"
            "⚠️ فقط ولت جداگانه با کمی ETH برای گس — نه ولت اصلی!\n"
            "پیام شما بلافاصله پاک می‌شود. برای انصراف /cancel")

    async def wallet_del(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        wid = int(q.data.split(":")[1])
        ok = self.db.delete_wallet(q.from_user.id, wid)
        self.db.audit(q.from_user.id, "wallet_delete", f"{wid} {ok}")
        await q.message.reply_text("حذف شد." if ok else "این ولت تسک فعال دارد؛ اول تسک را لغو کنید.")

    async def got_pk(self, u: Update, c):
        uid = u.effective_user.id
        text = u.message.text or ""
        try:
            await u.message.delete()
        except Exception:  # noqa: BLE001
            await u.message.reply_text("⚠️ نتوانستم پیام را پاک کنم؛ لطفاً خودتان آن را پاک کنید.")
        try:
            pk = normalize_private_key(text)
        except Exception:  # noqa: BLE001
            return await u.effective_chat.send_message("❌ کلید معتبر نیست (۶۴ کاراکتر hex). دوباره بفرستید یا /cancel")
        addr, blob = self.vault.encrypt(uid, pk)
        try:
            bal = await self.blocking(pf.check_wallet, addr)
        except pf.PreflightError as e:
            return await u.effective_chat.send_message(f"❌ {e}")
        self.db.add_wallet(uid, addr, blob)
        self.db.audit(uid, "wallet_add", addr)
        c.user_data.pop("state", None)
        warn = "" if bal >= pf.WARN_BALANCE_WEI else "\n⚠️ موجودی برای گس کم است؛ حداقل 0.0001 ETH روی Robinhood Chain واریز کنید."
        await u.effective_chat.send_message(f"✅ ولت ثبت شد:\n<code>{addr}</code>\nموجودی: {bal / 1e18:.6f} ETH{warn}",
                                            parse_mode=ParseMode.HTML, reply_markup=self.menu())

    # ------------------------------------------------------------------ OpenSea API key
    async def key(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        k = self.db.apikey(uid)
        s = "🔑 کلید API شخصی OpenSea\n"
        if k:
            exp = f"انقضا: {fmt_ts(k['expires_at'])}" if k["expires_at"] else "انقضا: نامشخص"
            s += f"وضعیت: ثبت شده ({k['source']}) — {exp}\n\n"
        else:
            s += "وضعیت: ثبت نشده\n\n"
        s += ("روش ۱ (پیشنهادی): دکمه «⚡ ساخت خودکار کلید API» در منو — کلید از اینترنت خود شما از OpenSea گرفته "
              "و مستقیم به ربات داده می‌شود.\n"
              "روش ۲: در مرورگر خود این دستور را اجرا یا کلید کامل را از opensea.io/settings/developer بگیرید و همین‌جا بفرستید:\n"
              "<code>curl -X POST https://api.opensea.io/api/v2/auth/keys</code>")
        await u.message.reply_text(s, parse_mode=ParseMode.HTML, reply_markup=IM([[IB("✍️ ارسال دستی کلید", callback_data="kset")]]))

    async def key_set(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        c.user_data["state"] = "await_key"
        await q.message.reply_text("کلید API (یا کل خروجی JSON دستور curl) را بفرستید. /cancel برای انصراف")

    async def store_key(self, u: Update, c, key, expires_at, source):
        uid = u.effective_user.id
        try:
            ok, why = await self.blocking(self.os.test_key, key)
        except OpenSeaError as e:
            return await u.effective_chat.send_message(f"⚠️ تست کلید ممکن نشد: {e}. بعداً دوباره امتحان کنید.")
        if not ok:
            return await u.effective_chat.send_message(f"❌ کلید توسط OpenSea رد شد: {why}")
        self.db.set_apikey(uid, self.vault.seal(uid, "apikey", key), expires_at, source)
        self.db.audit(uid, "apikey_set", source)
        c.user_data.pop("state", None)
        exp = f"\nانقضا: {fmt_ts(expires_at)}" if expires_at else ""
        await u.effective_chat.send_message("✅ کلید API تست و ذخیره شد." + exp, reply_markup=self.menu())

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
            return await u.effective_chat.send_message(f"OpenSea خطا داده: {key[4:]}\n(سقف ساخت کلید فوری ۲ عدد در روز برای هر IP است)")
        if not key:
            return await u.effective_chat.send_message("❌ کلید پیدا نشد. دوباره بفرستید یا /cancel")
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "manual")

    async def webapp_data(self, u: Update, c):
        data = u.effective_message.web_app_data.data
        key, exp = self.parse_key_text(data)
        if key and key.startswith("ERR:"):
            return await u.effective_chat.send_message(f"OpenSea خطا داده: {key[4:]}\n(سقف ساخت کلید فوری ۲ عدد در روز برای هر IP است)")
        if not key:
            return await u.effective_chat.send_message("❌ داده نامعتبر از صفحه کلید")
        await self.store_key(u, c, key, exp or int(time.time()) + 7 * 86400, "instant")

    # ------------------------------------------------------------------ tasks
    async def new_task(self, u: Update, c):
        uid = u.effective_user.id
        self.db.ensure_user(uid, u.effective_user.username)
        if (m := self.need_sub(uid)):
            return await u.message.reply_text(m)
        if not self.db.apikey(uid):
            return await u.message.reply_text("اول کلید API را ثبت کنید (🔑 کلید API).")
        if not self.db.wallets(uid):
            return await u.message.reply_text("اول یک ولت ثبت کنید (👛 ولت‌ها).")
        if len(self.db.tasks(uid, active_only=True)) >= self.cfg.max_active_tasks:
            return await u.message.reply_text(f"حداکثر {self.cfg.max_active_tasks} تسک فعال مجاز است.")
        c.user_data["state"] = "await_link"
        await u.message.reply_text("🔗 لینک کالکشن OpenSea (یا slug) را بفرستید.\nمثال: https://opensea.io/collection/aerent")

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
            return await u.message.reply_text(
                "این پیام لینک کالکشن نیست. فقط لینک را بفرستید، مثل:\nhttps://opensea.io/collection/aerent\n"
                "(یا فقط slug مثل aerent — برای انصراف /cancel)")
        msg = await u.message.reply_text("⏳ در حال بررسی دراپ…")
        try:
            key = self.user_key(uid)
            slug = await self.blocking(self.os.resolve_slug, link, key)
            drop = await self.blocking(self.os.drop, slug, key)
            nft = pf.check_drop(drop)
            await self.blocking(pf.check_chain_contract, nft)
        except (OpenSeaError, pf.PreflightError, VaultError) as e:
            return await msg.edit_text(f"❌ {e}")
        stages = pf.pick_stages(drop)
        c.user_data.pop("state", None)
        if not stages:
            return await msg.edit_text("❌ همهٔ مرحله‌های این دراپ تمام شده‌اند.")
        ok_st, lines = [], []
        for s in stages:
            why = pf.unsupported_reason(s)
            lim = pf.stage_wallet_limit(s)
            lines.append(f"{'🟢' if not why else '⛔'} <b>{esc(s['label'])}</b> — {self.kind_fa(s['kind'], s['type'])} — "
                         f"{self.price_fa(s['price_wei'])} — سقف هر ولت: {lim if lim else '?'} — {fmt_ts(s['start'])}"
                         + (f"\n   ↳ {esc(why)}" if why else ""))
            if not why:
                ok_st.append(s)
        txt = (f"<b>{esc(drop.get('collection_name') or slug)}</b>\nقرارداد: <code>{nft}</code>\n"
               f"عرضه: {drop.get('total_supply')}/{drop.get('max_supply')}\n\n" + "\n".join(lines))
        if not ok_st:
            return await msg.edit_text(txt + "\n\n❌ هیچ مرحلهٔ قابل‌مینتی ندارد.", parse_mode=ParseMode.HTML)
        c.user_data["plan"] = {"step": "wallet", "slug": slug, "nft": nft, "name": drop.get("collection_name") or slug,
                               "stages": ok_st}
        ws = self.db.wallets(uid)
        kb = [[IB(w["address"], callback_data=f"pw:{w['id']}")] for w in ws] + [[IB("✖️ انصراف", callback_data="px")]]
        await msg.edit_text(txt + "\n\n👛 با کدام ولت مینت شود؟ (ربات برای این ولت allowlist و سقف هر مرحله را چک می‌کند)",
                            parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    @staticmethod
    def kind_fa(kind, typ=None):
        return {"signed": "امضادار/Allowlist", "public": "عمومی/Public"}.get(kind, typ or "?")

    @staticmethod
    def price_fa(p):
        return "رایگان" if p == 0 else (f"{p / 1e18:.6f} ETH" if p else "?")

    async def plan_wallet(self, u: Update, c):
        """Per-stage pre-check for the chosen wallet: eligibility (SIWE), per-wallet limit, already minted, funds."""
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        p = c.user_data.get("plan")
        w = self.db.wallet(uid, int(q.data.split(":")[1]))
        if not (p and w):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        nft, slug = p["nft"], p["slug"]
        signed_any = any(s["kind"] == "signed" for s in p["stages"])
        m = await q.message.reply_text("⏳ پیش‌بررسی ولت: موجودی، تعداد مینت قبلی، سقف هر مرحله"
                                       + (", ورود به OpenSea با ولت و چک allowlist…" if signed_any else "…"))
        try:
            bal = await self.blocking(pf.check_wallet, w["address"])
            done = await self.blocking(pf.minted_count, nft, w["address"])
        except pf.PreflightError as e:
            return await m.edit_text(f"❌ {e}")
        pub = None
        if any(s["kind"] == "public" for s in p["stages"]):
            try:
                pub = await self.blocking(pf.onchain_public_drop, nft)
            except pf.PreflightError:
                pub = None
        el, el_err = {}, None
        if signed_any:
            try:
                pk = self.vault.decrypt(uid, w["address"], w["blob"])
                jwt = await self.blocking(self.os.wallet_jwt, pk)
                del pk
                el = await self.blocking(self.os.eligibility, slug, self.user_key(uid), jwt)
            except (OpenSeaError, VaultError) as ex:
                el_err = str(ex)[:80]
        for s in p["stages"]:
            s["limit"] = pf.stage_wallet_limit(s)
            s["note"] = None
            if s["kind"] == "public" and pub:
                s["limit"] = pub[3]                                 # the contract enforces the on-chain value
                if s["price_wei"] > 0 and pub[0] != s["price_wei"]:
                    s["note"] = f"قیمت روی زنجیره ({pub[0] / 1e18:.6f}) با OpenSea فرق دارد"
            s["eligible"] = True if s["kind"] == "public" else el.get(s["uuid"])     # public stages have no allowlist
            room = (s["limit"] - done) if s["limit"] else pf.API_MAX_QTY
            fund = ((bal - pf.WARN_BALANCE_WEI // 4) // s["price_wei"]) if s["price_wei"] > 0 else pf.API_MAX_QTY
            s["maxq"] = max(0, min(pf.API_MAX_QTY, room, fund))
            s["why_no"] = ("طبق OpenSea این ولت در allowlist این مرحله نیست" if s["eligible"] is False else
                           s["note"] if s["note"] else
                           f"سقف ولت پر است ({done} از {s['limit']})" if s["limit"] and room <= 0 else
                           "موجودی برای حتی ۱ عدد کافی نیست" if s["maxq"] < 1 else None)
            s["qty"] = None
        p.update(step="select", wid=w["id"], addr=w["address"], bal=bal, done=done, el_err=el_err)
        await self.render_select(m, p)

    def select_text(self, p):
        lines = [f"👛 <code>{p['addr']}</code>\nموجودی: {p['bal'] / 1e18:.6f} ETH — مینت‌های قبلی این ولت در این کالکشن: {p['done']}\n"]
        for s in p["stages"]:
            elig = {True: "✅ allowlist هست", False: "❌ allowlist نیست"}.get(s["eligible"], "⚠️ allowlist نامعلوم")
            if s["kind"] == "public":
                elig = "عمومی (allowlist ندارد)" if s["eligible"] is not False else elig
            mark = "☑️" if s["qty"] else ("⛔" if s["why_no"] else "⬜")
            lines.append(f"{mark} <b>{esc(s['label'])}</b> — {self.kind_fa(s['kind'])} — {self.price_fa(s['price_wei'])} — {fmt_ts(s['start'])}\n"
                         f"   {elig} | Limit {s['limit'] if s['limit'] else '?'} per wallet | "
                         + (f"↳ {esc(s['why_no'])}" if s["why_no"] else f"قابل مینت برای شما: حداکثر {s['maxq']}")
                         + (f"\n   🎯 انتخاب شما: <b>{s['qty']} عدد</b>" if s["qty"] else ""))
        if p.get("el_err"):
            lines.append(f"\n⚠️ چک allowlist ممکن نشد ({esc(p['el_err'])}); در زمان مینت دوباره چک می‌شود.")
        lines.append("\nℹ️ سقف هر ولت در SeaDrop <b>تجمعی</b> است: مینت‌های مراحل قبل از سقف مراحل بعد کم می‌شود.")
        lines.append("مرحله(ها) را انتخاب و تعداد هرکدام را مشخص کنید، بعد «➡️ ادامه».")
        return "\n".join(lines)

    async def render_select(self, msg, p, new=False):
        kb = []
        for i, s in enumerate(p["stages"]):
            if s["why_no"]:
                continue
            kb.append([IB(("☑️ " if s["qty"] else "⬜ ") + s["label"][:30] + (f" × {s['qty']}" if s["qty"] else ""),
                          callback_data=f"ps:{i}")])
        if any(s["qty"] for s in p["stages"]):
            kb.append([IB("➡️ ادامه (خلاصه و تأیید)", callback_data="pn")])
        kb.append([IB("✖️ انصراف", callback_data="px")])
        f = msg.reply_text if new else msg.edit_text
        await f(self.select_text(p), parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    @staticmethod
    def qty_options(maxq):
        return [n for n in (1, 2, 3, 5, 10, 20, 50) if n < maxq]

    async def plan_stage(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        p = c.user_data.get("plan")
        try:
            i = int(q.data.split(":")[1]); s = p["stages"][i]
            assert p["step"] in ("select", "qty") and not s["why_no"]
        except (TypeError, KeyError, IndexError, ValueError, AssertionError):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        if s["qty"]:                                             # tap on a selected stage = deselect
            s["qty"] = None
            return await self.render_select(q.message, p)
        p["step"] = "qty"; p["qi"] = i
        lim = s["limit"]
        kb = [[IB(str(n), callback_data=f"pq:{i}:{n}") for n in self.qty_options(s["maxq"])]] if self.qty_options(s["maxq"]) else []
        kb.append([IB(f"🔝 حداکثر ({s['maxq']})", callback_data=f"pq:{i}:max")])
        if s["maxq"] > 1:
            kb.append([IB("✍️ عدد دلخواه", callback_data=f"pq:{i}:c")])
        kb.append([IB("↩️ برگشت", callback_data="pb")])
        cost = "" if not s["price_wei"] else f"\nقیمت هر عدد: {s['price_wei'] / 1e18:.6f} ETH"
        await q.message.edit_text(
            f"🎯 <b>{esc(s['label'])}</b>\nLimit {lim if lim else '?'} per wallet — سقف هر ولت این مرحله {lim if lim else 'نامشخص'} عدد است"
            f" (شما قبلاً {p['done']} عدد مینت کرده‌اید).\nحداکثر قابل مینت برای شما: <b>{s['maxq']}</b>{cost}\n\n"
            "می‌خواهید در این مرحله چند عدد مینت کنید؟", parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    async def plan_qty(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        p = c.user_data.get("plan")
        try:
            _, i, v = q.data.split(":"); i = int(i); s = p["stages"][i]
            assert p["step"] == "qty" and p.get("qi") == i
        except (TypeError, KeyError, IndexError, ValueError, AssertionError):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        if v == "c":
            c.user_data["state"] = "await_qty"
            p["qmsg"] = q.message.message_id
            return await q.message.reply_text(f"عدد مورد نظر را بفرستید (۱ تا {s['maxq']}). برای انصراف /cancel")
        n = s["maxq"] if v == "max" else int(v)
        if not 1 <= n <= s["maxq"]:
            return await q.message.reply_text(f"عدد باید بین ۱ و {s['maxq']} باشد.")
        s["qty"] = n; p["step"] = "select"; p.pop("qi", None)
        await self.render_select(q.message, p)

    async def got_qty(self, u: Update, c):
        p = c.user_data.get("plan")
        t = (u.message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if not (p and p.get("step") == "qty" and p.get("qi") is not None):
            c.user_data.pop("state", None)
            return await u.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        s = p["stages"][p["qi"]]
        if not t.isdigit() or not 1 <= int(t) <= s["maxq"]:
            return await u.message.reply_text(f"❌ یک عدد بین ۱ و {s['maxq']} بفرستید (یا /cancel).")
        s["qty"] = int(t); p["step"] = "select"; p.pop("qi", None)
        c.user_data.pop("state", None)
        await self.render_select(u.message, p, new=True)

    async def plan_back(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        p = c.user_data.get("plan")
        if not p or "wid" not in p:
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        p["step"] = "select"; p.pop("qi", None); c.user_data.pop("state", None)
        await self.render_select(q.message, p)

    @staticmethod
    def project(p):
        """Expected outcome if every earlier stage mints in full (cumulative per-wallet limit, start order)."""
        run, out = p["done"], []
        for s in sorted((s for s in p["stages"] if s["qty"]), key=lambda s: s["start"]):
            allowed = (s["limit"] - run) if s["limit"] else s["qty"]
            eff = max(0, min(s["qty"], allowed))
            out.append((s, eff)); run += eff
        return out

    async def plan_next(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        p = c.user_data.get("plan")
        if not p or p.get("step") != "select" or not any(s["qty"] for s in p["stages"]):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        proj = self.project(p)
        total = sum(s["price_wei"] * s["qty"] for s, _ in proj)
        gas = pf.WARN_BALANCE_WEI * len(proj)
        lines, ok = [f"📋 <b>خلاصهٔ برنامهٔ مینت</b> — {esc(p['name'])}\n👛 <code>{p['addr']}</code>\n"], True
        for n, (s, eff) in enumerate(proj, 1):
            pr, qt = s["price_wei"], s["qty"]
            cost = "رایگان" if not pr else f"{pr / 1e18:.6f} × {qt} = {pr * qt / 1e18:.6f} ETH"
            lines.append(f"{n}) <b>{esc(s['label'])}</b> ({self.kind_fa(s['kind'])}) — {fmt_ts(s['start'])}\n"
                         f"   تعداد: <b>{qt}</b> — {cost}")
            if eff < s["qty"]:
                lines.append(f"   ⚠️ اگر مراحل قبلی کامل مینت شوند، به‌خاطر سقف تجمعی ({s['limit']}) در این مرحله فقط "
                             f"{eff} عدد ممکن است. ربات لحظهٔ شلیک دوباره از روی زنجیره حساب می‌کند و هرگز بیشتر از سقف نمی‌فرستد.")
        lines.append(f"\n💰 جمع قیمت (حداکثر): {total / 1e18:.6f} ETH + گس (~{gas / 1e18:.4f}) | موجودی: {p['bal'] / 1e18:.6f} ETH")
        if p["bal"] < total + gas:
            lines.append("⚠️ موجودی برای همهٔ برنامه کافی نیست؛ ربات در هر مرحله فقط به اندازهٔ موجودی همان لحظه مینت می‌کند.")
        k = self.db.apikey(uid)
        last = max(s["start"] for s, _ in proj)
        if k["expires_at"] and k["expires_at"] < max(last, time.time()) + 300:
            ok = False
            lines.append(f"❌ کلید API قبل از شروع آخرین مرحله منقضی می‌شود ({fmt_ts(k['expires_at'])}); کلید جدید بگیرید.")
        usr = self.db.user(uid)
        if usr and usr["sub_until"] < last:
            ok = False
            lines.append(f"❌ اشتراک شما قبل از شروع آخرین مرحله تمام می‌شود ({fmt_ts(usr['sub_until'])}); اول تمدید کنید.")
        if len(self.db.tasks(uid, active_only=True)) + len(proj) > self.cfg.max_active_tasks:
            ok = False
            lines.append(f"❌ با این برنامه از سقف {self.cfg.max_active_tasks} تسک فعال رد می‌شوید.")
        lines.append("\n🔒 ربات در هر مرحله دقیقاً قیمت اعلام‌شده × تعداد را می‌پردازد، نه بیشتر؛ اگر سازنده قیمت را بالا ببرد "
                     "چیزی ارسال نمی‌شود. تعداد هم فقط ممکن است کمتر شود (سقف ولت / عرضه / موجودی)، هرگز بیشتر.")
        p["step"] = "summary"
        kb = ([[IB("🚀 تأیید و مسلح کردن", callback_data="pa")]] if ok else []) + \
             [[IB("✏️ ویرایش", callback_data="pb"), IB("✖️ انصراف", callback_data="px")]]
        await q.message.edit_text("\n".join(lines) + ("\n\nتأیید می‌کنید؟" if ok else "\n\nبرنامه قابل ثبت نیست."),
                                  parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    async def plan_arm(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        p = c.user_data.get("plan")
        if not p or p.get("step") != "summary":
            return await q.message.reply_text("این برنامه قبلاً ثبت شده یا منقضی شده است.")
        if (m := self.need_sub(uid)):
            return await q.message.reply_text(m)
        p["step"] = "arming"                                     # double taps hit the guard above
        sel = sorted((s for s in p["stages"] if s["qty"]), key=lambda s: s["start"])
        items = [{"label": s["label"], "start": s["start"], "end": s["end"], "kind": s["kind"],
                  "price_wei": s["price_wei"], "quantity": s["qty"]} for s in sel]
        plan_id = os.urandom(6).hex()
        res = self.db.add_plan(uid, p["wid"], p["slug"], p["nft"], items, self.cfg.max_active_tasks, plan_id)
        if res in ("dup", "limit"):
            p["step"] = "summary"
            return await q.message.reply_text("برای یکی از این مرحله‌ها با این ولت قبلاً تسک فعال دارید." if res == "dup"
                                              else "سقف تسک فعال پر است.")
        self.db.audit(uid, "plan_add", f"{plan_id} {p['slug']} " + ", ".join(f"{t}:{s['label']}x{s['qty']}" for t, s in zip(res, sel)))
        c.user_data.pop("plan", None)
        rows = []
        for t, s in zip(res, sel):
            when = "همین الان آماده می‌شود" if s["start"] <= time.time() + self.cfg.spawn_before_s else \
                f"{self.cfg.spawn_before_s // 60} دقیقه قبل از شروع آماده می‌شود"
            rows.append(f"• تسک #{t}: {s['label']} × {s['qty']} — {fmt_ts(s['start'])} ({when})")
        await q.message.reply_text("🚀 مسلح شد. ربات منتظر لحظهٔ شروع هر مرحله می‌ماند و همان لحظه شلیک می‌کند:\n"
                                   + "\n".join(rows) + "\n\nنتیجهٔ هر مرحله همین‌جا اطلاع داده می‌شود.", reply_markup=self.menu())

    async def plan_cancel(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        c.user_data.pop("plan", None); c.user_data.pop("state", None)
        await q.message.reply_text("لغو شد.", reply_markup=self.menu())

    async def stale(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        await q.message.reply_text("این دکمه مربوط به نسخهٔ قبلی است؛ دوباره «🎯 تسک جدید» را بزنید.")

    async def tasks(self, u: Update, c):
        uid = u.effective_user.id
        ts = self.db.tasks(uid)
        if not ts:
            return await u.message.reply_text("تسکی ندارید.")
        icon = {"scheduled": "🕒", "running": "⚙️", "minted": "✅", "failed": "❌", "cancelled": "🚫", "ended": "⌛", "not_eligible": "⛔"}
        lines, kb = [], []
        for t in ts[:10]:
            lines.append(f"{icon.get(t['status'], '•')} #{t['id']} {esc(t['slug'])} / {esc(t['stage_label'])} × {t['quantity']} — {fmt_ts(t['start_ts'])}"
                         + (f"\n   {esc(t['result'])}" if t["result"] else ""))
            if t["status"] in ("scheduled", "running"):
                kb.append([IB(f"🚫 لغو #{t['id']}", callback_data=f"tc:{t['id']}")])
        await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=IM(kb) if kb else None)

    async def task_cancel(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        ok = await self.blocking(self.runner.cancel, q.from_user.id, int(q.data.split(":")[1]))
        await q.message.reply_text("لغو شد." if ok else "قابل لغو نیست.")

    # ------------------------------------------------------------------ text router
    async def text(self, u: Update, c):
        t = u.message.text
        routes = {B_SUB: self.sub, B_WAL: self.wallets, B_KEY: self.key, B_NEW: self.new_task, B_TASKS: self.tasks, B_HELP: self.help}
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
                "select": "مرحله‌ها را با دکمه‌های پیام قبلی انتخاب کنید و بعد «➡️ ادامه» را بزنید",
                "qty": "تعداد را با دکمه‌ها انتخاب کنید (برای عدد دلخواه «✍️ عدد دلخواه» را بزنید)",
                "summary": "برنامه هنوز ثبت نشده؛ «🚀 تأیید و مسلح کردن» یا «✖️ انصراف» را بزنید",
                "arming": "در حال ثبت برنامه…"}.get(step)
        if hint:
            return await u.message.reply_text(hint + " (یا /cancel).")
        # a pasted private key outside the flow: delete it anyway
        if len(t) in (64, 66) and all(ch in "0123456789abcdefABCDEFx" for ch in t):
            try:
                await u.message.delete()
            except Exception:  # noqa: BLE001
                pass
            return await u.effective_chat.send_message("⚠️ پیام شبیه کلید خصوصی بود و پاک شد. برای ثبت ولت از «👛 ولت‌ها» اقدام کنید.")
        await u.message.reply_text("از منو استفاده کنید.", reply_markup=self.menu())

    # ------------------------------------------------------------------ admin
    def is_admin(self, u):
        return u.effective_user.id in self.cfg.admin_ids

    async def grant(self, u: Update, c):
        if not self.is_admin(u):
            return
        try:
            uid, days = int(c.args[0]), float(c.args[1])
        except (IndexError, ValueError):
            return await u.message.reply_text("/grant <user_id> <days>")
        new = self.db.extend_sub(uid, days)
        self.db.audit(u.effective_user.id, "admin_grant", f"{uid} {days}")
        await u.message.reply_text(f"ok, until {fmt_ts(new)}")

    async def ban(self, u: Update, c):
        if not self.is_admin(u):
            return
        try:
            uid, v = int(c.args[0]), int(c.args[1]) if len(c.args) > 1 else 1
        except (IndexError, ValueError):
            return await u.message.reply_text("/ban <user_id> [1|0]")
        self.db.ensure_user(uid)
        self.db.x("UPDATE users SET banned=? WHERE id=?", (v, uid))
        await u.message.reply_text("ok")

    async def stats(self, u: Update, c):
        if not self.is_admin(u):
            return
        q = lambda s, a=(): self.db.q(s, a)[0][0]
        now = int(time.time())
        active = q("SELECT COUNT(*) FROM tasks WHERE status IN ('scheduled','running')")
        minted = q("SELECT COUNT(*) FROM tasks WHERE status='minted'")
        failed = q("SELECT COUNT(*) FROM tasks WHERE status='failed'")
        await u.message.reply_text(
            f"users {q('SELECT COUNT(*) FROM users')} | active subs {q('SELECT COUNT(*) FROM users WHERE sub_until>?', (now,))}\n"
            f"tasks active {active} | minted {minted} | failed {failed}\n"
            f"stars {q('SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0')}")

    # ------------------------------------------------------------------ wiring
    def build(self) -> Application:
        b = ApplicationBuilder().token(self.cfg.bot_token).concurrent_updates(True)
        if self.cfg.telegram_base_url:
            b = b.base_url(self.cfg.telegram_base_url.rstrip("/") + "/bot").base_file_url(self.cfg.telegram_base_url.rstrip("/") + "/file/bot")
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
        app.add_handler(CallbackQueryHandler(self.buy, pattern=r"^buy$"))
        app.add_handler(CallbackQueryHandler(self.wallet_add, pattern=r"^wadd$"))
        app.add_handler(CallbackQueryHandler(self.wallet_del, pattern=r"^wdel:\d+$"))
        app.add_handler(CallbackQueryHandler(self.key_set, pattern=r"^kset$"))
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
                await c.bot.send_message(update.effective_chat.id, "⚠️ خطای داخلی؛ دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass

    async def _post_init(self, app):
        self.loop = asyncio.get_running_loop()
        self._runner_task = asyncio.create_task(self.runner.loop())

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
