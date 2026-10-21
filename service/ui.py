"""Presentation layer for the Telegram bot (Bot API 10.3 features, verified live 2026-10-21 against @RaySea_Bot):

* custom emoji in messages (<tg-emoji>) and on buttons (icon_custom_emoji_id)  - needs the bot owner's Premium
* coloured buttons (style = success | danger | primary), disabled buttons, copy-to-clipboard buttons
* <tg-time> date entities (shown in each user's own time zone / relative), expandable block quotes
* message effects (private chats only)

Everything degrades safely: if Telegram ever rejects premium-only content (e.g. the owner's Premium lapses), the
SafeBot wrapper strips custom emoji and retries once, then keeps premium features off for the process.
Emoji ids come from Telegram's own packs "NewsEmoji" and "TgPremiumIcons" (getStickerSet).
"""
import datetime as dt
import html
import os
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ExtBot

PREMIUM = os.environ.get("UI_PREMIUM", "1") != "0"

# name -> (custom_emoji_id, fallback emoji)
EMOJI = {
    "bolt": ("5456140674028019486", "⚡"), "stop": ("5260293700088511294", "⛔"), "bang": ("5274099962655816924", "❗"),
    "warn": ("5447644880824181073", "⚠️"), "chart": ("5231200819986047254", "📊"), "up": ("5244837092042750681", "📈"),
    "ok": ("5206607081334906820", "✅"), "no": ("5210952531676504517", "❌"), "bell": ("5458603043203327669", "🔔"),
    "pin": ("5397782960512444700", "📌"), "usd": ("5409048419211682843", "💵"), "red": ("5411225014148014586", "🔴"),
    "green": ("5416081784641168838", "🟢"), "next": ("5416117059207572332", "➡️"), "fire": ("5424972470023104089", "🔥"),
    "search": ("5231012545799666522", "🔍"), "shield": ("5251203410396458957", "🛡"), "link": ("5271604874419647061", "🔗"),
    "info": ("5334544901428229844", "ℹ️"), "refresh": ("5375338737028841420", "🔄"), "top": ("5415655814079723871", "🔝"),
    "new": ("5382357040008021292", "🆕"), "plus": ("5397916757333654639", "➕"), "gem": ("5427168083074628963", "💎"),
    "star": ("5438496463044752972", "⭐"), "spark": ("5325547803936572038", "✨"), "trash": ("5445267414562389170", "🗑"),
    "lock": ("5296369303661067030", "🔒"), "gear": ("5341715473882955310", "⚙️"), "wait": ("5386367538735104399", "⏳"),
    "cal": ("5413879192267805083", "🗓"), "idea": ("5422439311196834318", "💡"), "gold": ("5440539497383087970", "🥇"),
    "silver": ("5447203607294265305", "🥈"), "bronze": ("5453902265922376865", "🥉"), "free": ("5406756500108501710", "🆓"),
    "edit": ("5395444784611480792", "✏️"), "siren": ("5395695537687123235", "🚨"), "party": ("5461151367559141950", "🎉"),
    "robot": ("5983582264502523326", "🤖"), "timer": ("5897490345133872272", "⏱"), "list": ("5897517150024766102", "📝"),
    "zap": ("5784989466431067159", "⚡"), "coin": ("5996797032763760544", "🪙"), "deal": ("5944927588903294140", "🤝"),
    "gift": ("5938345530047337186", "🎁"), "pstar": ("5942783678668085067", "⭐"),
}
# message effects (ids verified with sendMessage on 2026-10-21; an invalid id returns EFFECT_ID_INVALID)
EFFECT_PARTY = "5046509860389126442"
EFFECT_FIRE = "5104841245755180586"

EXPLORER = os.environ.get("EXPLORER_URL", "https://robinhoodchain.blockscout.com").rstrip("/")
SEP = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄"


def esc(s):
    return html.escape(str(s), quote=False)


def e(name):
    """Inline emoji: animated custom emoji when premium is on, the plain emoji otherwise."""
    cid, ch = EMOJI[name]
    return f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>' if PREMIUM else ch


def plain(name):
    return EMOJI[name][1]


# ------------------------------------------------------------------ text helpers
def fa_rel(ts, now=None):
    rel = ts - (now or time.time())
    if rel <= 0:
        return "شروع شده"
    if rel < 3600:
        return f"{max(1, round(rel / 60))} دقیقه دیگر"
    if rel < 86400:
        return f"{rel / 3600:.1f} ساعت دیگر"
    return f"{rel / 86400:.1f} روز دیگر"


def when(ts, rel=True):
    """Absolute date/time in the reader's own time zone (+ live relative time). Fallback text is UTC."""
    ts = int(ts)
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    s = f'<tg-time unix="{ts}" format="wDT">{d:%Y-%m-%d %H:%M} UTC</tg-time>'
    if rel:
        s += f' · <tg-time unix="{ts}" format="r">{fa_rel(ts)}</tg-time>'
    return s


def eth(wei, digits=6):
    return f"{wei / 1e18:.{digits}f} ETH"


def price(wei):
    return "رایگان" if wei == 0 else (eth(wei) if wei else "نامشخص")


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else str(addr)


def code(s):
    return f"<code>{esc(s)}</code>"


def title(icon, text):
    return f"{e(icon)} <b>{text}</b>"


def quote(text, expandable=False):
    return f"<blockquote{' expandable' if expandable else ''}>{text}</blockquote>"


def kv(label, value):
    return f"<b>{label}:</b> {value}"


def bar(done, total, width=10):
    if not total:
        return ""
    k = max(0, min(width, round(width * done / total)))
    return "▰" * k + "▱" * (width - k)


# ------------------------------------------------------------------ buttons
def btn(text, cb=None, *, url=None, style=None, icon=None, copy=None, disabled=False, web_app=None):
    """Inline button. style: success (green) / danger (red) / primary (blue). icon: EMOJI name."""
    kw = {}
    if style:
        kw["style"] = style
    if icon:
        if PREMIUM:
            kw["icon_custom_emoji_id"] = EMOJI[icon][0]
        else:
            text = f"{EMOJI[icon][1]} {text}"
    if copy is not None:
        kw["copy_text"] = {"text": str(copy)[:256]}
    if disabled:
        kw["disabled"] = {}
    if copy is not None or disabled:
        return InlineKeyboardButton(text, api_kwargs=kw)
    return InlineKeyboardButton(text, callback_data=cb, url=url, web_app=web_app, api_kwargs=kw or None)


def kb(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def rbtn(text, icon=None, style=None, web_app=None):
    kw = {}
    if style:
        kw["style"] = style
    if icon:
        if PREMIUM:
            kw["icon_custom_emoji_id"] = EMOJI[icon][0]
        else:
            text = f"{EMOJI[icon][1]} {text}"
    return KeyboardButton(text, web_app=web_app, api_kwargs=kw or None)


def reply_kb(rows, placeholder=None):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=(placeholder or None) and placeholder[:64])


def tx_url(tx):
    return f"{EXPLORER}/tx/{tx}"


def addr_url(a):
    return f"{EXPLORER}/address/{a}"


# ------------------------------------------------------------------ premium-safe bot
_EMOJI_TAG = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def strip_premium(data):
    """Copy of a Bot API payload without premium-only content (custom emoji in text, emoji icons on buttons).
    Returns None when there was nothing to strip."""
    changed = False
    out = dict(data)
    for k in ("text", "caption"):
        v = out.get(k)
        if isinstance(v, str) and "<tg-emoji" in v:
            out[k] = _EMOJI_TAG.sub(r"\1", v)
            changed = True
    rm = out.get("reply_markup")
    if rm is not None:
        d = rm.to_dict() if hasattr(rm, "to_dict") else rm
        if isinstance(d, dict):
            rows_key = "inline_keyboard" if "inline_keyboard" in d else ("keyboard" if "keyboard" in d else None)
            if rows_key:
                new_rows = []
                for row in d[rows_key]:
                    nr = []
                    for b in row:
                        if isinstance(b, dict) and "icon_custom_emoji_id" in b:
                            b = {k: v for k, v in b.items() if k != "icon_custom_emoji_id"}
                            changed = True
                        nr.append(b)
                    new_rows.append(nr)
                d = dict(d, **{rows_key: new_rows})
                out["reply_markup"] = d
    return out if changed else None


class SafeBot(ExtBot):
    """ExtBot that retries once without premium-only content if Telegram rejects a request carrying it."""

    async def _do_post(self, endpoint, data, **kw):
        global PREMIUM
        if endpoint in ("sendMessage", "editMessageText") and "link_preview_options" not in data:
            data = dict(data, link_preview_options={"is_disabled": True})     # cards carry explorer/OpenSea links
        try:
            return await super()._do_post(endpoint, data, **kw)
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                raise
            alt = strip_premium(data)
            if alt is None:
                raise
            res = await super()._do_post(endpoint, alt, **kw)
            if PREMIUM:
                PREMIUM = False
                import logging
                logging.getLogger("ui").warning("premium content rejected (%s) -> premium UI disabled", str(ex)[:120])
            return res
