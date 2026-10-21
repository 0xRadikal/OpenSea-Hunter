"""service/rich.py: rich-HTML builder, classic fallback text, RTL isolation, transport fallback rules."""
import asyncio

import pytest
from telegram import Message, User
from telegram.error import BadRequest, EndPointNotFound, NetworkError

from service import rich as R, ui
from service.rich import Doc, lt, mono
from tests.test_ui import valid_html


@pytest.fixture(autouse=True)
def premium_on(monkeypatch):
    monkeypatch.setattr(ui, "PREMIUM", True)


def sample():
    return (Doc().h("عنوان " + R.esc("<x>"), "ok").p(f"متن {lt('Limit 1 per wallet')}").hr()
            .kv([("ولت", mono("0x" + "a" * 40)), None, ("قیمت", R.price(10 ** 15))], caption="جزئیات")
            .table(["#", "نام"], [["1", lt("OG & co")]], caption="مرحله‌ها")
            .checklist([(True, "یک"), (False, "دو")]).ul(["a", None, "b"]).ol(["x"])
            .quote("خط۱\nخط۲", expandable=True, cite="منبع").note("نکته", "warn")
            .details("بیشتر", "<p>داخل<br>دوم</p>").math(r"1 \times 2 = 2", "1×2=2").footer("پایان"))


def test_doc_rich_and_classic_are_valid_and_escaped():
    d = sample()
    assert valid_html(d)
    h = d.html()
    assert "<h2>" in h and "<hr/>" in h and "<table bordered striped compact><caption>جزئیات</caption>" in h
    assert '<input type="checkbox" checked>یک' in h and "<tg-math-block>1 \\times 2 = 2</tg-math-block>" in h
    assert "<blockquote expandable>خط۱<br>خط۲<cite>منبع</cite></blockquote>" in h
    assert "عنوان &lt;x&gt;" in h
    assert "OG &amp; co" in h and "<td align=\"center\" valign=\"middle\">1</td>" in h
    c = d.classic()
    assert "<table" not in c and "<input" not in c and "<br" not in c and "✅</tg-emoji> یک" in c and "1×2=2" in c


def test_rtl_isolation_and_payload():
    assert lt("Limit 1") == "\u2066Limit 1\u2069" and mono("0xab") == "<code>\u20660xab\u2069</code>"
    p = Doc().p("x").payload()
    assert p == {"html": "<p>x</p>", "is_rtl": True, "skip_entity_detection": True}


def test_price_and_num_never_scientific():
    assert R.num(10 ** 13) == "0.00001" and R.num(0) == "0" and R.num(123 * 10 ** 14) == "0.0123"
    assert R.price(0) == "رایگان" and R.price(None) == "نامشخص" and "0.001000 ETH" in R.price(10 ** 15)


def test_classic_cut_keeps_html_balanced():
    d = Doc()
    for i in range(400):
        d.p(f"<b>سطر {i}</b> <code>{'x' * 10}</code>")
    c = d.classic()
    assert len(c) <= 4000 and "\n…" in c[-40:] and "سطر 399" not in c
    assert valid_html(c)


def test_empty_blocks_are_skipped():
    assert Doc().kv([None]).table(["a"], []).ul([None]).html() == ""


# ------------------------------------------------------------------ transport
class _Bot:
    """Minimal stand-in recording what rich.send / rich.edit call (real PTB methods are covered live + in e2e)."""

    def __init__(self, rich_exc=None):
        self.rich_exc, self.calls = rich_exc, []

    async def do_api_request(self, ep, api_kwargs=None, return_type=None):
        self.calls.append((ep, api_kwargs))
        if self.rich_exc:
            raise self.rich_exc
        return "rich"

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("sendMessage", text, kw))
        return "classic"

    async def edit_message_text(self, text, **kw):
        self.calls.append(("editMessageText", text, kw))
        if text is None and self.rich_exc:
            raise self.rich_exc
        return "classic" if text else "rich"


def run(c):
    return asyncio.run(c)


def test_send_rich_ok():
    b = _Bot()
    assert run(R.send(b, 5, Doc().p("hi"), effect="1")) == "rich"
    ep, kw = b.calls[0]
    assert ep == "sendRichMessage" and kw["rich_message"]["is_rtl"] is True and kw["message_effect_id"] == "1"
    assert "reply_markup" not in kw and len(b.calls) == 1


@pytest.mark.parametrize("exc", [EndPointNotFound("Not Found"), BadRequest("Bad Request: can't parse rich message")])
def test_send_falls_back_to_classic_on_refusal(exc):
    b = _Bot(exc)
    assert run(R.send(b, 5, Doc().h("T").p("hi"))) == "classic"
    assert b.calls[1][0] == "sendMessage" and b.calls[1][1] == "<b>T</b>\n\nhi"


@pytest.mark.parametrize("exc", [BadRequest("Bad Request: chat not found"), NetworkError("timed out"),
                                 BadRequest("Forbidden: bot was blocked by the user")])
def test_send_does_not_mask_real_errors(exc):
    b = _Bot(exc)
    with pytest.raises(type(exc)):
        run(R.send(b, 5, Doc().p("hi")))
    assert len(b.calls) == 1                       # no duplicate classic message


def test_edit_paths():
    assert run(R.edit(_Bot(), 5, 9, Doc().p("hi"))) == "rich"
    assert run(R.edit(_Bot(BadRequest("Bad Request: message is not modified")), 5, 9, Doc().p("hi"))) is None
    b = _Bot(BadRequest("Bad Request: unsupported rich message"))
    assert run(R.edit(b, 5, 9, Doc().p("hi"))) == "classic" and b.calls[-1][1] == "hi"
    with pytest.raises(BadRequest):
        run(R.edit(_Bot(BadRequest("Bad Request: message to edit not found")), 5, 9, Doc().p("hi")))


def test_send_through_real_ptb_bot(monkeypatch):
    """The real PTB request path: sendRichMessage endpoint + JSON-encoded rich_message, Message parsed back."""
    import telegram._bot as tb
    seen = []

    async def fake(self, endpoint, data, **kw):
        seen.append((endpoint, dict(data)))
        return {"message_id": 3, "date": 0, "chat": {"id": 5, "type": "private"}}

    monkeypatch.setattr(tb.Bot, "_do_post", fake)

    async def go():
        b = ui.SafeBot("123:x"); b._initialized = True; b._bot_user = User(1, "x", True)
        m = await R.send(b, 5, Doc().p("سلام"), markup=ui.kb([ui.btn("x", "y")]))
        await R.edit(b, 5, 3, Doc().p("ویرایش"))
        return m
    m = run(go())
    assert isinstance(m, Message) and m.message_id == 3
    assert seen[0][0] == "sendRichMessage" and seen[0][1]["rich_message"]["html"] == "<p>سلام</p>"
    assert seen[1][0] == "editMessageText" and seen[1][1]["rich_message"]["html"] == "<p>ویرایش</p>"
    assert "text" not in seen[1][1]


def test_premium_refusal_strips_custom_emoji_inside_rich_html(monkeypatch):
    import telegram._bot as tb
    seen = []

    async def fake(self, endpoint, data, **kw):
        seen.append(dict(data))
        if len(seen) == 1:
            raise BadRequest("Bad Request: custom emoji are available only for premium")
        return {"message_id": 3, "date": 0, "chat": {"id": 5, "type": "private"}}

    monkeypatch.setattr(tb.Bot, "_do_post", fake)

    async def go():
        b = ui.SafeBot("123:x"); b._initialized = True; b._bot_user = User(1, "x", True)
        return await R.send(b, 5, Doc().h("سلام", "ok"))
    assert run(go()).message_id == 3
    assert "<tg-emoji" in seen[0]["rich_message"]["html"] and "<tg-emoji" not in seen[1]["rich_message"]["html"]
    assert seen[1]["rich_message"]["is_rtl"] is True and ui.PREMIUM is False


def test_every_table_cell_is_centered():
    import re
    h = sample().html()
    cells = re.findall(r"<t[dh][^>]*>", h)
    assert cells and all(c.endswith(' align="center" valign="middle">') for c in cells), cells


def test_note_is_pull_quote_with_optional_icon_and_cite():
    d = Doc().note("a<br>b", None, cite="X").note("c", "warn")
    assert d.html().startswith("<aside>a<br>b<cite>X</cite></aside><aside><tg-emoji")
    assert "<blockquote>a\nb — X</blockquote>" in d.classic()
