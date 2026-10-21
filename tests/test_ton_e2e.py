"""Bot-level TON payment flow: real bot code <-> fake Telegram + fake TonAPI (no blockchain needed)."""
import asyncio
import json
import threading
import time

import pytest

from service import ton_pay as T
from tests.fake_telegram import FakeTelegram
from tests.fake_tonapi import FakeTonApi

ADDR = "UQDz3fDX55jDBLWnhRKX2K7KppTtm2YmCrEdxwrWcb-E32rx"
RAW = T.raw_address(ADDR)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    import service.bot as botmod
    from service.config import Config
    from service.vault import create_master_key
    tg, api = FakeTelegram(), FakeTonApi(RAW)
    api.pay(T.NANO, "pre-existing history")                     # must never be credited
    d = tmp_path_factory.mktemp("ton")
    cfg = Config(); cfg.data_dir = str(d / "data"); cfg.master_key_file = str(d / "m.key"); create_master_key(cfg.master_key_file)
    cfg.bot_token = "123456:TEST"; cfg.telegram_base_url = tg.url; cfg.admin_ids = {7}; cfg.price_stars = 500
    cfg.ton_address, cfg.ton_api_key, cfg.ton_api_base, cfg.price_ton = ADDR, "testkey", api.url, "5"
    svc = botmod.Service(cfg)
    svc.ton.POLL_BUSY = svc.ton.POLL_IDLE = 1                    # production: 10 s / 60 s (keeps the test fast)
    app = svc.build()
    loop = asyncio.new_event_loop()

    async def boot():
        await app.initialize(); await svc._post_init(app)
        await app.updater.start_polling(poll_interval=0.05, timeout=1); await app.start()
    threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(boot()), loop.run_forever()),
                     daemon=True).start()
    time.sleep(2.5)                                              # first poll (cursor init) runs at startup
    yield dict(tg=tg, api=api, svc=svc)

    async def down():
        await app.updater.stop(); await app.stop(); await svc._post_shutdown(app); await app.shutdown()
    asyncio.run_coroutine_threadsafe(down(), loop).result(20)
    loop.call_soon_threadsafe(loop.stop); tg.close(); api.close()


def buttons(msg):
    return [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]


def test_ton_payment_flow(world):
    tg, api, svc = world["tg"], world["api"], world["svc"]
    U = 6001
    assert svc.db.setting("ton_last_lt") is not None                                  # startup poll initialised the cursor
    m = tg.mark(); tg.text(U, "💳 اشتراک"); sub = tg.wait_text(U, "اشتراک فعال ندارید", since=m)[1]
    assert "500 ⭐ Stars یا 5.0 TON" in sub["text"]
    assert [b.get("callback_data") for b in buttons(sub)] == ["buy", "tonpay"]
    m = tg.mark(); tg.callback(U, "tonpay")
    photo = tg.wait(lambda c: c[0] == "sendPhoto" and str(c[1].get("chat_id")) == str(U), since=m)[1]
    inv_msg = tg.wait_text(U, "پرداخت با TON", since=m)[1]
    inv = svc.db.q("SELECT * FROM ton_invoices WHERE user_id=?", (U,))[0]
    amt = T.fmt_ton(inv["amount_nano"])
    assert inv["memo"] in photo["caption"] and amt in photo["caption"]
    t = inv_msg["text"]
    assert f"مبلغ | {amt} TON (≈ $" in t and f"آدرس | {ADDR}" in t and f"کامنت (Memo) | {inv['memo']}" in t
    b = buttons(inv_msg)
    assert b[0]["url"] == f"https://app.tonkeeper.com/transfer/{ADDR}?amount={inv['amount_nano']}&text={inv['memo']}"
    assert [x["copy_text"]["text"] for x in b[1:4]] == [ADDR, amt, inv["memo"]]
    assert b[4]["callback_data"] == f"tonchk:{inv['id']}"
    # tapping «tonpay» again reuses the same invoice (same amount/memo)
    m = tg.mark(); tg.callback(U, "tonpay"); tg.wait_text(U, "پرداخت با TON", since=m)
    assert svc.db.q("SELECT COUNT(*) FROM ton_invoices WHERE user_id=?", (U,))[0][0] == 1
    # check before paying -> "not found yet" alert, nothing credited
    m = tg.mark(); tg.callback(U, f"tonchk:{inv['id']}")
    a = tg.wait(lambda c: c[0] == "answerCallbackQuery" and "هنوز پرداختی" in str(c[1].get("text")), since=m)[1]
    assert str(a.get("show_alert")).lower() == "true" and not svc.db.is_subscribed(U)
    # someone else's invoice id -> refused
    m = tg.mark(); tg.callback(U + 1, f"tonchk:{inv['id']}")
    tg.wait(lambda c: c[0] == "answerCallbackQuery" and "فاکتور پیدا نشد" in str(c[1].get("text")), since=m)
    # partial payment, then the rest -> automatic activation + admin notified
    m = tg.mark(); api.pay(2 * T.NANO, inv["memo"])
    part = tg.wait_text(U, "بخشی از مبلغ دریافت شد", since=m, timeout=30)[1]["text"]
    assert "باقی‌مانده | 3.0 TON" in part
    m = tg.mark(); h = api.pay(3 * T.NANO, f"for {inv['memo']}")
    ok = tg.wait_text(U, "پرداخت TON تأیید شد", since=m, timeout=30)[1]
    assert "دوره | 30 روز" in ok["text"] and ok.get("message_effect_id")
    assert buttons(ok)[0]["url"] == T.tx_url(h)
    tg.wait_text(7, "TON paid", since=m)
    assert svc.db.is_subscribed(U)
    # «check» after payment -> confirmed alert
    m = tg.mark(); tg.callback(U, f"tonchk:{inv['id']}")
    tg.wait(lambda c: c[0] == "answerCallbackQuery" and "پرداخت شده" in str(c[1].get("text")), since=m)
    # unknown transfer -> admins get /tonassign hint; admin credits it once
    V = 6002
    m = tg.mark(); h2 = api.pay(4 * T.NANO, "no code here")
    tg.wait_text(7, "TON unmatched", since=m, timeout=30)
    m = tg.mark(); tg.text(V, f"/tonassign {h2} {V}"); time.sleep(1)                 # non-admin ignored
    assert not svc.db.is_subscribed(V)
    m = tg.mark(); tg.text(7, f"/tonassign {h2} {V}"); tg.wait_text(7, "credited to", since=m)
    assert svc.db.is_subscribed(V)
    m = tg.mark(); tg.text(7, f"/tonassign {h2} {V}"); tg.wait_text(7, "not an unmatched", since=m)
    # admin status + price change + disable
    m = tg.mark(); tg.text(7, "/tonstatus"); st = tg.wait_text(7, "received", since=m)[1]["text"]
    assert "received 9.0 TON" in st and "unmatched 0" in st and "paid 1" in st       # 5 (invoice) + 4 (manual)
    m = tg.mark(); tg.text(7, "/settonprice 0"); tg.wait_text(7, "TON price = OFF", since=m)
    m = tg.mark(); tg.text(V + 1, "💳 اشتراک"); s2 = tg.wait_text(V + 1, "اشتراک فعال ندارید", since=m)[1]
    assert [b.get("callback_data") for b in buttons(s2)] == ["buy"]
    m = tg.mark(); tg.text(7, "/settonprice ۷٫۵"); tg.wait_text(7, "TON price = 7.5", since=m)
    # pre-existing history transfer was never credited to anyone
    assert svc.db.q("SELECT COUNT(*) FROM ton_payments WHERE kind='history'")[0][0] == 1
    out = json.dumps([p for _, p in tg.calls], ensure_ascii=False, default=str)
    assert "testkey" not in out                                                         # API key never leaks
