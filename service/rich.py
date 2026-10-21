"""Rich Messages (Bot API 10.1-10.3) for the bot UI — verified live on @RaySea_Bot 2026-10-21:

* sendRichMessage / editMessageText(rich_message=...) with is_rtl=True -> the whole message is laid out right-to-left
  (Persian), tables/headings/dividers/details/footer/LaTeX render as native blocks, inline keyboards + custom emoji +
  message effects all work, a plain message can be edited into a rich one and back.
* Latin fragments (addresses, tx hashes, "Limit 1 per wallet", stage names) are isolated with U+2066..U+2069 so they
  never flip the surrounding Persian text (Telegram keeps these characters verbatim).

A screen is a `Doc` (list of rich-HTML blocks). `Doc.html()` -> rich HTML; `Doc.classic()` -> the same content as
classic Bot-API HTML (used if Telegram ever rejects a rich message, e.g. an older Bot API server in tests).
"""
import html as _html
import re

from . import ui

LRI, PDI, RLM = "\u2066", "\u2069", "\u200f"


def esc(s):
    return _html.escape(str(s), quote=False)


def attr(s):
    return _html.escape(str(s), quote=True)


def ltr(s):
    """Isolate a left-to-right fragment (address, hash, English label, number+unit) inside Persian text."""
    return f"{LRI}{s}{PDI}"


def lt(s):
    """Escaped + isolated Latin text."""
    return ltr(esc(s))


def mono(s):
    return f"<code>{ltr(esc(s))}</code>"


def tm(ts, fmt="wDT"):
    """Date/time entity in the reader's own time zone; UTC fallback text."""
    import datetime as dt
    ts = int(ts)
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return f'<tg-time unix="{ts}" format="{fmt}">{d:%Y-%m-%d %H:%M} UTC</tg-time>'


def rel(ts):
    return f'<tg-time unix="{int(ts)}" format="r">{esc(ui.fa_rel(ts))}</tg-time>'


def when(ts):
    return f"{tm(ts)} · {rel(ts)}"


def eth(wei, digits=6):
    return ltr(f"{wei / 1e18:.{digits}f} ETH")


def num(wei):
    """wei -> plain decimal ETH string, no scientific notation, trailing zeros trimmed."""
    v = f"{wei / 1e18:.8f}".rstrip("0").rstrip(".")
    return v or "0"


def price(wei):
    return "رایگان" if wei == 0 else (eth(wei) if wei else "نامشخص")


E = ui.e   # inline custom emoji (falls back to the plain emoji when premium is off)


class Doc:
    """Builder for one rich screen. Every method appends one block and returns self."""

    def __init__(self):
        self.blocks = []        # (kind, rich_html, classic_html)

    def _add(self, kind, rich, classic):
        self.blocks.append((kind, rich, classic))
        return self

    def h(self, text, icon=None, level=2):
        t = (E(icon) + " " if icon else "") + text
        return self._add("h", f"<h{level}>{t}</h{level}>", f"<b>{t}</b>")

    def p(self, text):
        return self._add("p", f"<p>{text}</p>", text)

    def hr(self):
        return self._add("hr", "<hr/>", ui.SEP)

    def quote(self, text, expandable=False, cite=None):
        c = f"<cite>{cite}</cite>" if cite else ""
        rich = f"<blockquote{' expandable' if expandable else ''}>{text.replace(chr(10), '<br>')}{c}</blockquote>"
        return self._add("q", rich, f"<blockquote{' expandable' if expandable else ''}>{text}</blockquote>")

    def note(self, text, icon="info"):
        """Highlighted pull-quote style hint."""
        return self._add("note", f"<aside>{E(icon)} {text}</aside>", f"<blockquote>{E(icon)} {text}</blockquote>")

    def ul(self, items):
        items = [i for i in items if i]
        if not items:
            return self
        return self._add("ul", "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>",
                         "\n".join(f"• {i}" for i in items))

    def ol(self, items):
        items = [i for i in items if i]
        return self._add("ol", "<ol>" + "".join(f"<li>{i}</li>" for i in items) + "</ol>",
                         "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1)))

    def checklist(self, items):
        """items: [(done: bool, text)] -> native task list."""
        rich = "<ul>" + "".join(f'<li><input type="checkbox"{" checked" if d else ""}>{t}</li>' for d, t in items) + "</ul>"
        classic = "\n".join(f"{E('ok') if d else E('timer')} {t}" for d, t in items)
        return self._add("check", rich, classic)

    def kv(self, rows, caption=None, striped=True):
        """Two-column key/value table (Persian label | value)."""
        rows = [r for r in rows if r]
        if not rows:
            return self
        cap = f"<caption>{caption}</caption>" if caption else ""
        body = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
        rich = f"<table bordered{' striped' if striped else ''} compact>{cap}{body}</table>"
        classic = (f"<b>{caption}</b>\n" if caption else "") + "\n".join(f"<b>{k}:</b> {v}" for k, v in rows)
        return self._add("table", rich, classic)

    def table(self, head, rows, caption=None, align=None):
        """Multi-column table. align: list of 'right'|'center'|'left' per column."""
        if not rows:
            return self
        al = align or [None] * len(head)
        cell = lambda tag, v, a: f"<{tag}{f' align={chr(34)}{a}{chr(34)}' if a else ''}>{v}</{tag}>"
        cap = f"<caption>{caption}</caption>" if caption else ""
        rich = (f"<table bordered striped compact>{cap}<tr>" + "".join(cell("th", h, a) for h, a in zip(head, al)) + "</tr>"
                + "".join("<tr>" + "".join(cell("td", v, a) for v, a in zip(r, al)) + "</tr>" for r in rows) + "</table>")
        classic = (f"<b>{caption}</b>\n" if caption else "") + "\n".join(
            " · ".join(f"{v}" for v in r) for r in rows)
        return self._add("table", rich, classic)

    def details(self, summary, inner_html, open_=False, inner_classic=None):
        rich = f"<details{' open' if open_ else ''}><summary>{summary}</summary>{inner_html}</details>"
        return self._add("details", rich, f"<b>{summary}</b>\n<blockquote expandable>{inner_classic or strip_tags(inner_html)}</blockquote>")

    def math(self, tex, classic):
        return self._add("math", f"<tg-math-block>{esc(tex)}</tg-math-block>", classic)

    def footer(self, text):
        return self._add("footer", f"<footer>{text}</footer>", f"<i>{text}</i>")

    def extend(self, other):
        self.blocks.extend(other.blocks)
        return self

    # ---------------------------------------------------------------- output
    def html(self):
        return "".join(r for _, r, _ in self.blocks)

    def classic(self):
        out = []
        for kind, _, c in self.blocks:
            if kind == "hr" and out and out[-1] == ui.SEP:
                continue
            out.append(c)
        txt = classic_safe("\n\n".join(x for x in out if x))
        return _cut(re.sub(r"\n{3,}", "\n\n", txt), 3900)

    def payload(self):
        return {"html": self.html(), "is_rtl": True, "skip_entity_detection": True}


_TAG = re.compile(r"<(/?)([a-z0-9-]+)[^>]*>")
_CLASSIC_OK = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-emoji", "tg-time", "tg-spoiler"}


def _cut(h, n):
    """Trim classic HTML to ~n chars at a line break without leaving a tag/entity split or unclosed."""
    if len(h) <= n:
        return h
    h = h[:n]
    h = h[:h.rfind("\n")] if "\n" in h else h
    h = re.sub(r"<[^>]*$|&[a-z#0-9]*$", "", h)
    stack = []
    for m in _TAG.finditer(h):
        close, tag = m.group(1), m.group(2)
        if close:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    return h + "\n…" + "".join(f"</{t}>" for t in reversed(stack))


def classic_safe(h):
    """Last pass for classic Bot-API HTML: <br> -> newline, checkboxes -> emoji, any non-classic tag dropped."""
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r'<input type="checkbox" checked>', "✅ ", h)
    h = re.sub(r'<input type="checkbox">', "▫️ ", h)
    return _TAG.sub(lambda m: m.group(0) if m.group(2) in _CLASSIC_OK else "", h)


def strip_tags(h):
    """Rich HTML fragment -> classic-safe HTML (keeps classic tags, turns block tags into line breaks)."""
    h = re.sub(r"</?(p|li|tr|h[1-6]|ul|ol|table|caption|aside|footer|details|summary)[^>]*>", "\n", h)
    h = re.sub(r"<br\s*/?>|<hr\s*/?>", "\n", h)
    h = re.sub(r"<(td|th)[^>]*>", " ", h)
    h = re.sub(r"</(td|th)>", " ", h)
    h = re.sub(r'<input type="checkbox" checked>', "✅ ", h)
    h = re.sub(r'<input type="checkbox">', "▫️ ", h)
    h = re.sub(r"<cite>(.*?)</cite>", r" — \1", h, flags=re.S)
    h = re.sub(r"<tg-math(-block)?>(.*?)</tg-math(-block)?>", r"\2", h, flags=re.S)
    h = _TAG.sub(lambda m: m.group(0) if m.group(2) in _CLASSIC_OK else "", h)
    return re.sub(r"\n\s*\n+", "\n", h).strip()


# ------------------------------------------------------------------ transport
async def send(bot, chat_id, doc, markup=None, effect=None):
    """sendRichMessage (RTL); falls back to classic HTML sendMessage if rich messages are rejected."""
    from telegram import Message
    from telegram.constants import ParseMode
    kw = {"chat_id": chat_id, "rich_message": doc.payload()}
    if markup is not None:
        kw["reply_markup"] = markup
    if effect:
        kw["message_effect_id"] = effect
    try:
        return await bot.do_api_request("sendRichMessage", api_kwargs=kw, return_type=Message)
    except Exception as ex:  # noqa: BLE001
        if not _fallback_ok(ex):
            raise
        _log_fallback(ex)
        return await bot.send_message(chat_id, doc.classic(), parse_mode=ParseMode.HTML, reply_markup=markup,
                                      message_effect_id=effect)


async def edit(bot, chat_id, message_id, doc, markup=None):
    from telegram.constants import ParseMode
    try:
        return await bot.edit_message_text(None, chat_id=chat_id, message_id=message_id, reply_markup=markup,
                                           api_kwargs={"rich_message": doc.payload()})
    except Exception as ex:  # noqa: BLE001
        if "not modified" in str(ex).lower():
            return None
        if not _fallback_ok(ex):
            raise
        _log_fallback(ex)
        return await bot.edit_message_text(doc.classic(), chat_id=chat_id, message_id=message_id,
                                           parse_mode=ParseMode.HTML, reply_markup=markup)


def _fallback_ok(ex):
    """Only Telegram-side refusals of the rich payload trigger the classic fallback (not network errors, not
    'chat not found' / 'message to edit not found', which would fail the same way)."""
    from telegram.error import BadRequest, EndPointNotFound
    if isinstance(ex, EndPointNotFound):
        return True
    if isinstance(ex, BadRequest):
        s = str(ex).lower()
        return not any(k in s for k in ("chat not found", "message to edit not found", "bot was blocked",
                                        "message can't be edited"))
    return False


def _log_fallback(ex):
    import logging
    logging.getLogger("rich").warning("rich message refused (%s) -> classic HTML", str(ex)[:160])
