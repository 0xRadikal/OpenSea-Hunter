"""UI layer (service/ui.py) + result cards: Bot API 10.3 payload shapes, premium fallback, HTML safety."""
import asyncio
import html.parser
import re

import pytest
from telegram import User
from telegram.error import BadRequest

from service import ui
from service.runner import classify, result_card


class _Check(html.parser.HTMLParser):
    """Telegram accepts only a small HTML subset and needs balanced tags; check both."""
    OK = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-emoji", "tg-time", "tg-spoiler"}

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.OK, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, (tag, self.stack)
        self.stack.pop()


def valid_html(s):
    p = _Check(); p.feed(s); p.close()
    assert not p.stack, p.stack
    return True


@pytest.fixture(autouse=True)
def premium_on(monkeypatch):
    monkeypatch.setattr(ui, "PREMIUM", True)


def test_buttons_payload_shapes():
    b = ui.btn("تأیید", "pa", style="success", icon="ok").to_dict()
    assert b == {"text": "تأیید", "callback_data": "pa", "style": "success", "icon_custom_emoji_id": ui.EMOJI["ok"][0]}
    assert ui.btn("x", copy="0xabc").to_dict() == {"text": "x", "copy_text": {"text": "0xabc"}}
    assert ui.btn("x", disabled=True).to_dict() == {"text": "x", "disabled": {}}
    assert ui.btn("x", url="https://a.b").to_dict() == {"text": "x", "url": "https://a.b"}
    assert ui.rbtn("🎯 تسک جدید", style="primary").to_dict() == {"text": "🎯 تسک جدید", "style": "primary"}
    rk = ui.reply_kb([[ui.rbtn("a")]], "p" * 100).to_dict()
    assert rk["is_persistent"] and rk["resize_keyboard"] and len(rk["input_field_placeholder"]) == 64


def test_premium_off_falls_back_to_plain_emoji(monkeypatch):
    monkeypatch.setattr(ui, "PREMIUM", False)
    assert ui.e("ok") == "✅"
    assert ui.btn("تأیید", "pa", icon="ok").to_dict() == {"text": "✅ تأیید", "callback_data": "pa"}


def test_when_uses_tg_time_with_utc_fallback():
    s = ui.when(1800000000)
    assert '<tg-time unix="1800000000" format="wDT">2027-01-15 08:00 UTC</tg-time>' in s and 'format="r"' in s
    assert valid_html(s)


def test_strip_premium_and_safebot_retry(monkeypatch):
    data = {"chat_id": 1, "text": f"hi {ui.e('ok')} <b>x</b>",
            "reply_markup": ui.kb([ui.btn("a", "a", icon="ok", style="success")])}
    out = ui.strip_premium(data)
    assert out["text"] == "hi ✅ <b>x</b>"
    assert out["reply_markup"]["inline_keyboard"][0][0] == {"text": "a", "callback_data": "a", "style": "success"}
    assert ui.strip_premium({"text": "plain"}) is None

    import telegram._bot as tb
    sent = []

    async def fake(self, endpoint, data, **kw):
        d = data
        sent.append((endpoint, d))
        if len(sent) == 1:
            raise BadRequest("Bad Request: some premium-only rejection")
        return {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "ok"}

    monkeypatch.setattr(tb.Bot, "_do_post", fake)

    async def go():
        b = ui.SafeBot("123:x"); b._initialized = True; b._bot_user = User(1, "x", True)
        await b.send_message(1, data["text"], parse_mode="HTML", reply_markup=data["reply_markup"])
    asyncio.run(go())
    assert len(sent) == 2 and "<tg-emoji" in sent[0][1]["text"] and "<tg-emoji" not in sent[1][1]["text"]
    assert sent[0][1]["link_preview_options"] == {"is_disabled": True}
    assert ui.PREMIUM is False                                    # premium switched off for the process


def test_safebot_does_not_retry_unrelated_errors(monkeypatch):
    import telegram._bot as tb
    calls = []

    async def fake(self, endpoint, data, **kw):
        d = data
        calls.append(1)
        raise BadRequest("Bad Request: chat not found")
    monkeypatch.setattr(tb.Bot, "_do_post", fake)

    async def go():
        b = ui.SafeBot("123:x"); b._initialized = True; b._bot_user = User(1, "x", True)
        await b.send_message(1, "plain text, nothing premium")
    with pytest.raises(BadRequest):
        asyncio.run(go())
    assert len(calls) == 1


def test_result_card_minted_and_failed():
    t = {"id": 7, "slug": "naz<ism>", "stage_label": "Public & co"}
    st, detail = classify({"minted": True, "tx": "0x" + "ab" * 32, "token_ids": [4], "wanted": 1,
                           "speed": {"api_win": 0.3, "sent": 0.4, "receipt": 0.6, "rank": 2}})
    txt, mk = result_card(t, st, detail, {"tx": "0x" + "ab" * 32})
    assert valid_html(txt) and "naz&lt;ism&gt;" in txt and "Public &amp; co" in txt and "tx: " not in txt
    btns = [b for row in mk.to_dict()["inline_keyboard"] for b in row]
    assert btns[0]["url"].endswith("/tx/0x" + "ab" * 32) and btns[1]["copy_text"]["text"] == "0x" + "ab" * 32
    assert btns[2]["url"] == "https://opensea.io/collection/naz<ism>"
    txt, mk = result_card(t, "failed", "قیمت عوض شد <script>", {})
    assert valid_html(txt) and "&lt;script&gt;" in txt and mk is None


def test_bot_screens_render_valid_html(tmp_path):
    """Every screen builder returns Telegram-valid HTML (balanced, whitelisted tags) — premium on and off."""
    from service.bot import Service
    from service.config import Config
    from service.vault import create_master_key
    cfg = Config(); cfg.data_dir = str(tmp_path / "d"); cfg.master_key_file = str(tmp_path / "m.key")
    create_master_key(cfg.master_key_file); cfg.bot_token = "1:x"
    svc = Service(cfg)
    uid = 42
    svc.db.ensure_user(uid, "u"); svc.db.extend_sub(uid, 3)
    for premium in (True, False):
        ui.PREMIUM = premium
        for txt, _ in (svc.wallets_view(uid), svc.key_menu(uid), svc.settings_view(uid), svc.tasks_view(uid)):
            assert valid_html(txt), txt
        assert valid_html(svc.progress_card(uid)[1])
        p = {"addr": "0x" + "1" * 40, "bal": 10 ** 16, "done": 0, "el_err": "x<y", "stages": [
            {"label": "OG <List>", "kind": "signed", "price_wei": 0, "start": 1800000000, "eligible": True, "limit": 2,
             "why_no": None, "maxq": 2, "qty": 1},
            {"label": "Pub", "kind": "public", "price_wei": 10 ** 15, "start": 1800003600, "eligible": False, "limit": 1,
             "why_no": "طبق OpenSea این ولت در allowlist این مرحله نیست", "maxq": 0, "qty": None}]}
        txt = svc.select_text(p)
        assert valid_html(txt) and "OG &lt;List&gt;" in txt and "x&lt;y" in txt
    assert re.search(r"tg-emoji", svc.select_text(p)) is None    # last loop iteration ran with premium off
