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
        self.runner = Runner(self.db, self.vault, cfg, self.notify, extra_runner_args)

    # ------------------------------------------------------------------ infra
    def notify(self, uid, text):
        """Thread-safe (runner runs in an executor thread)."""
        if self.loop and self.app:
            asyncio.run_coroutine_threadsafe(self._send(uid, text), self.loop)

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
            "سلام 👋\nاین ربات مینت امضادار (Allowlist / OG) دراپ‌های OpenSea روی Robinhood Chain را با سرعت بالا "
            "و خودکار برای ولت شما انجام می‌دهد.\n\n"
            "مراحل: ۱) اشتراک ۲) ثبت ولت ۳) ثبت کلید API شخصی OpenSea ۴) ارسال لینک کالکشن.\n"
            "از «❓ راهنما» توضیحات کامل و نکات امنیتی را بخوانید.", reply_markup=self.menu())

    async def help(self, u: Update, c):
        await u.message.reply_text(
            "📘 راهنما\n\n"
            "• فقط مراحل رایگان و امضادار (signed presale، قیمت ۰) روی Robinhood Chain پشتیبانی می‌شود. "
            "ربات هرگز تراکنش پولی/پابلیک ارسال نمی‌کند (value=0 قفل است).\n"
            "• ولت شما باید در allowlist همان مرحله باشد؛ ربات قبل از ثبت تسک با ورود (SIWE) به OpenSea "
            "واجد شرایط بودن را چک می‌کند.\n"
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

    async def got_link(self, u: Update, c):
        uid = u.effective_user.id
        msg = await u.message.reply_text("⏳ در حال بررسی دراپ…")
        try:
            key = self.user_key(uid)
            slug = await self.blocking(self.os.resolve_slug, u.message.text, key)
            drop = await self.blocking(self.os.drop, slug, key)
            nft = pf.check_drop(drop)
            await self.blocking(pf.check_chain_contract, nft)
        except (OpenSeaError, pf.PreflightError, VaultError) as e:
            return await msg.edit_text(f"❌ {e}")
        stages = pf.pick_stages(drop)
        if not stages:
            return await msg.edit_text("❌ مرحله‌ای که هنوز تمام نشده باشد ندارد.")
        free = [s for s in stages if s["free_signed"]]
        lines = [f"{'🟢' if s['free_signed'] else '⛔'} {esc(s['label'])} — {s['type']} — قیمت {s['price']} wei — "
                 f"{fmt_ts(s['start'])}" for s in stages]
        txt = (f"<b>{esc(drop.get('collection_name') or slug)}</b>\nقرارداد: <code>{nft}</code>\n"
               f"عرضه: {drop.get('total_supply')}/{drop.get('max_supply')}\n\n" + "\n".join(lines))
        if not free:
            c.user_data.pop("state", None)
            return await msg.edit_text(txt + "\n\n❌ مرحلهٔ رایگانِ امضادار ندارد (فقط این نوع پشتیبانی می‌شود).", parse_mode=ParseMode.HTML)
        c.user_data.update(state=None, slug=slug, nft=nft, stages=free)
        kb = [[IB(f"🎯 {s['label'][:40]}", callback_data=f"st:{i}")] for i, s in enumerate(free)]
        await msg.edit_text(txt + "\n\nمرحله را انتخاب کنید:", parse_mode=ParseMode.HTML, reply_markup=IM(kb))

    async def pick_stage(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        try:
            st = c.user_data["stages"][int(q.data.split(":")[1])]
        except (KeyError, IndexError, ValueError):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        c.user_data["stage"] = st
        ws = self.db.wallets(q.from_user.id)
        kb = [[IB(w["address"], callback_data=f"tw:{w['id']}")] for w in ws]
        await q.message.reply_text(f"مرحله: {st['label']}\nولت را انتخاب کنید:", reply_markup=IM(kb))

    async def pick_wallet(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        st, slug, nft = c.user_data.get("stage"), c.user_data.get("slug"), c.user_data.get("nft")
        w = self.db.wallet(uid, int(q.data.split(":")[1]))
        if not (st and slug and w):
            return await q.message.reply_text("جلسه منقضی شد؛ دوباره «🎯 تسک جدید» را بزنید.")
        if self.db.duplicate_task(uid, w["id"], nft, st["label"]):
            return await q.message.reply_text("برای این ولت و مرحله قبلاً تسک فعال دارید.")
        m = await q.message.reply_text("⏳ پیش‌بررسی: موجودی، NFT فعلی، ورود به OpenSea با ولت و چک allowlist…")
        report, ok = [], True
        try:
            bal = await self.blocking(pf.check_wallet, w["address"])
            report.append(f"{'✅' if bal >= pf.WARN_BALANCE_WEI else '⚠️'} موجودی گس: {bal / 1e18:.6f} ETH")
            if bal < pf.WARN_BALANCE_WEI // 4:
                ok = False
                report.append("❌ موجودی برای گس کافی نیست")
            held = await self.blocking(pf.nft_balance, nft, w["address"])
            report.append(f"ℹ️ این ولت الان {held} عدد از این NFT دارد (ربات دقیقاً ۱ عدد دیگر مینت می‌کند)")
        except pf.PreflightError as e:
            return await m.edit_text(f"❌ {e}")
        try:
            pk = self.vault.decrypt(uid, w["address"], w["blob"])
            jwt = await self.blocking(self.os.wallet_jwt, pk)
            del pk
            el = await self.blocking(self.os.eligibility, slug, self.user_key(uid), jwt)
            e = el.get(st["uuid"])
            if e is True:
                report.append("✅ ورود به OpenSea با ولت موفق — ولت در allowlist این مرحله هست")
            elif e is False:
                ok = False
                report.append("❌ طبق OpenSea این ولت در allowlist این مرحله نیست")
            else:
                report.append("⚠️ وضعیت allowlist این مرحله از OpenSea برنگشت (در زمان مینت دوباره چک می‌شود)")
        except (OpenSeaError, VaultError) as ex:
            report.append(f"⚠️ چک allowlist ممکن نشد ({esc(str(ex)[:80])}); در زمان مینت چک می‌شود")
        k = self.db.apikey(uid)
        if k["expires_at"] and k["expires_at"] < st["start"] + 300:
            ok = False
            report.append(f"❌ کلید API قبل از شروع مرحله منقضی می‌شود ({fmt_ts(k['expires_at'])}); "
                          "نزدیک‌تر به دراپ کلید جدید بگیرید و دوباره تسک بسازید")
        txt = f"🎯 {esc(slug)} / {esc(st['label'])}\nشروع: {fmt_ts(st['start'])}\nولت: <code>{w['address']}</code>\n\n" + "\n".join(report)
        if not ok:
            return await m.edit_text(txt + "\n\nتسک ساخته نشد.", parse_mode=ParseMode.HTML)
        c.user_data["confirm"] = {"wid": w["id"]}
        await m.edit_text(txt + "\n\nتأیید می‌کنید؟", parse_mode=ParseMode.HTML,
                          reply_markup=IM([[IB("✅ ثبت تسک", callback_data="tok"), IB("✖️ انصراف", callback_data="tno")]]))

    async def confirm(self, u: Update, c):
        q = u.callback_query
        await q.answer()
        uid = q.from_user.id
        d = c.user_data
        if q.data == "tno" or not d.get("confirm"):
            d.clear()
            return await q.message.reply_text("لغو شد.", reply_markup=self.menu())
        if (m := self.need_sub(uid)):
            return await q.message.reply_text(m)
        st, wid = d["stage"], d["confirm"]["wid"]
        if self.db.duplicate_task(uid, wid, d["nft"], st["label"]):
            return await q.message.reply_text("این تسک قبلاً ثبت شده.")
        if len(self.db.tasks(uid, active_only=True)) >= self.cfg.max_active_tasks:
            return await q.message.reply_text("سقف تسک فعال پر است.")
        tid = self.db.add_task(uid, wid, d["slug"], d["nft"], st["label"], st["start"], st["end"])
        self.db.audit(uid, "task_add", f"{tid} {d['slug']} {st['label']}")
        d.clear()
        await q.message.reply_text(f"✅ تسک #{tid} ثبت شد. ربات {self.cfg.spawn_before_s // 60} دقیقه قبل از شروع آماده می‌شود "
                                   "و نتیجه را همین‌جا اطلاع می‌دهد.", reply_markup=self.menu())

    async def tasks(self, u: Update, c):
        uid = u.effective_user.id
        ts = self.db.tasks(uid)
        if not ts:
            return await u.message.reply_text("تسکی ندارید.")
        icon = {"scheduled": "🕒", "running": "⚙️", "minted": "✅", "failed": "❌", "cancelled": "🚫", "ended": "⌛", "not_eligible": "⛔"}
        lines, kb = [], []
        for t in ts[:10]:
            lines.append(f"{icon.get(t['status'], '•')} #{t['id']} {esc(t['slug'])} / {esc(t['stage_label'])} — {fmt_ts(t['start_ts'])}"
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
            c.user_data.pop("state", None)
            return await routes[t](u, c)
        if st == "await_key":
            return await self.got_key(u, c)
        if st == "await_link":
            return await self.got_link(u, c)
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
        app.add_handler(CallbackQueryHandler(self.pick_stage, pattern=r"^st:\d+$"))
        app.add_handler(CallbackQueryHandler(self.pick_wallet, pattern=r"^tw:\d+$"))
        app.add_handler(CallbackQueryHandler(self.confirm, pattern=r"^t(ok|no)$"))
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
