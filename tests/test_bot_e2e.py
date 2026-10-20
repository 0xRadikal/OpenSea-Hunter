"""Full product E2E: real bot code <-> fake Telegram Bot API, fake OpenSea (real SIWE semantics), anvil fork.
Covers: subscription via Stars (incl. duplicate delivery + wrong amount), wallet add (message deleted, encrypted),
API key via WebApp, link -> stage -> wallet -> SIWE eligibility -> task -> automatic mint -> notification,
non-eligible wallet refused, group chat ignored, no secret ever leaves in a bot message or sits in plaintext on disk."""
import asyncio, datetime as dt, json, os, threading, time
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea
from tests.fake_telegram import FakeTelegram

pytestmark = pytest.mark.fork


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    fu.start_fork(); ts = fu.register_test_signer()
    fo, tg = FakeOpenSea(), FakeTelegram()
    os.environ["OSH_READ_RPCS"] = fu.F
    import importlib, service.preflight as pf
    importlib.reload(pf)
    import service.bot as botmod
    importlib.reload(botmod)
    from service.config import Config
    from service.vault import create_master_key
    d = tmp_path_factory.mktemp("svc")
    cfg = Config(); cfg.data_dir = str(d / "data"); cfg.master_key_file = str(d / "m.key"); create_master_key(cfg.master_key_file)
    cfg.bot_token = "123456:TEST"; cfg.telegram_base_url = tg.url; cfg.opensea_base = fo.url; cfg.admin_ids = {7}
    cfg.keypage_url = "https://example.org/keypage/"; cfg.spawn_before_s = 3600; cfg.price_stars = 500
    svc = botmod.Service(cfg, extra_runner_args=["--api-base", fo.url, "--rpc", fu.F])
    app = svc.build()
    loop = asyncio.new_event_loop()

    async def boot():
        await app.initialize(); await svc._post_init(app)
        await app.updater.start_polling(poll_interval=0.05, timeout=1); await app.start()
    th = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(boot()), loop.run_forever()), daemon=True)
    th.start()
    tg.wait(lambda c: True, timeout=1) if tg.calls else time.sleep(1.5)
    yield dict(ts=ts, fo=fo, tg=tg, svc=svc, cfg=cfg)

    async def down():
        await app.updater.stop(); await app.stop(); await svc._post_shutdown(app); await app.shutdown()
    asyncio.run_coroutine_threadsafe(down(), loop).result(20)
    loop.call_soon_threadsafe(loop.stop); fo.close(); tg.close(); fu.stop_fork()


def subscribe(tg, uid):
    m = tg.mark(); tg.text(uid, "💳 اشتراک"); tg.wait_text(uid, "اشتراک فعال ندارید", since=m)
    m = tg.mark(); tg.callback(uid, "buy")
    inv = tg.wait(lambda c: c[0] == "sendInvoice", since=m)[1]
    assert inv["currency"] == "XTR" and inv["prices"][0]["amount"] == 500 and inv.get("provider_token", "") == ""
    m = tg.mark(); tg.precheckout(uid, inv["payload"], 1)                       # tampered amount -> refused
    assert str(tg.wait(lambda c: c[0] == "answerPreCheckoutQuery", since=m)[1]["ok"]).lower() == "false"
    m = tg.mark(); tg.precheckout(uid, inv["payload"], 500)
    assert str(tg.wait(lambda c: c[0] == "answerPreCheckoutQuery", since=m)[1]["ok"]).lower() == "true"
    m = tg.mark(); tg.paid(uid, inv["payload"], 500, f"charge-{uid}"); tg.wait_text(uid, "پرداخت موفق", since=m)


def add_wallet(tg, uid, pk):
    m = tg.mark(); tg.callback(uid, "wadd"); tg.wait_text(uid, "کلید خصوصی", since=m)
    m = tg.mark(); mid = tg.text(uid, pk)
    tg.wait(lambda c: c[0] == "deleteMessage" and str(c[1]["message_id"]) == str(mid))
    tg.wait_text(uid, "ولت ثبت شد", since=m)


def test_full_flow(world):
    tg, fo, ts, svc = world["tg"], world["fo"], world["ts"], world["svc"]
    U, BAD = 5001, 5002
    a, pk = fu.funded_wallet(); b, pkb = fu.funded_wallet()
    start = int(time.time()) + 50
    slug = "e2e"
    fo.drops[slug] = {"collection_slug": slug, "collection_name": "E2E Drop", "chain": "robinhood", "drop_type": "seadrop_v1_erc721",
                      "contract_address": fu.NFT, "is_minting": False, "total_supply": "50", "max_supply": "1000",
                      "stages": [{"uuid": "s-og", "label": "OG List", "stage_type": "signed_presale", "price": "0",
                                  "price_currency_address": "0x" + "0" * 40, "max_per_wallet": "1",
                                  "start_time": iso(start), "end_time": iso(start + 3600)},
                                 {"uuid": "s-pub", "label": "Public", "stage_type": "public_sale", "price": str(10**17),
                                  "price_currency_address": "0x" + "0" * 40, "max_per_wallet": "5",
                                  "start_time": iso(start + 3600), "end_time": iso(start + 7200)}]}
    fo.contracts[fu.NFT] = slug
    fo.eligible.add(a.address.lower())
    cd = fu.signed_calldata(ts, a.address, 0xE2E, start=start, end=start + 3600)
    fo.mint[slug] = lambda m: (200, {"to": fu.SD, "data": cd, "value": "0", "chain": "robinhood"}) if m.lower() == a.address.lower() \
        else (422, {"errors": ["Wallet is not eligible for the active drop stage, 'OG List' (signed_presale): the wallet is not on this stage's allowlist."]})

    m = tg.mark(); tg.text(U, "/start"); tg.wait_text(U, "سلام", since=m)
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "اشتراک فعال لازم", since=m)        # gate
    m = tg.mark(); tg.callback(U, "wadd"); tg.wait_text(U, "اشتراک فعال لازم", since=m)          # gate
    subscribe(tg, U)
    exp1 = svc.db.user(U)["sub_until"]
    m = tg.mark(); tg.paid(U, f"sub:30:{U}", 500, f"charge-{U}"); time.sleep(1.0)                # duplicate delivery
    assert svc.db.user(U)["sub_until"] == exp1

    add_wallet(tg, U, pk)
    m = tg.mark(); tg.webapp(U, json.dumps({"api_key": "bad" * 8, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait_text(U, "رد شد", since=m)
    m = tg.mark(); tg.webapp(U, json.dumps({"api_key": "k" * 32, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait_text(U, "تست و ذخیره شد", since=m)

    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, f"https://opensea.io/item/robinhood/{fu.NFT}/3")
    msg = tg.wait_text(U, "مرحله را انتخاب کنید", since=m)[1]
    assert "OG List" in msg["text"] and "🟢 <b>Public</b>" in msg["text"]                       # paid public now selectable
    kb = msg["reply_markup"]["inline_keyboard"]; assert len(kb) == 2 and "OG List" in kb[0][0]["text"]
    m = tg.mark(); tg.callback(U, kb[0][0]["callback_data"]); w = tg.wait_text(U, "ولت را انتخاب کنید", since=m)[1]
    m = tg.mark(); tg.callback(U, w["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
    rep = tg.wait_text(U, "تأیید می‌کنید", since=m, timeout=40)[1]["text"]
    assert "allowlist این مرحله هست" in rep
    m = tg.mark(); tg.callback(U, "tok"); tg.callback(U, "tok"); tg.callback(U, "tok")       # triple tap
    tg.wait_text(U, "ثبت شد", since=m); time.sleep(1.0)
    assert len(svc.db.tasks(U)) == 1, [dict(t) for t in svc.db.tasks(U)]

    # runner picks it up, engine mints at T0 on the fork, user gets notified
    tg.wait_text(U, "مینت انجام شد", timeout=150)
    assert fu.holds(a.address) == 1
    assert svc.db.tasks(U)[0]["status"] == "minted"

    # ---- non-eligible user is refused before any task exists
    subscribe(tg, BAD); add_wallet(tg, BAD, pkb)
    m = tg.mark(); tg.text(BAD, "🔑 کلید API"); tg.wait_text(BAD, "ثبت نشده", since=m)
    m = tg.mark(); tg.callback(BAD, "kset"); tg.wait_text(BAD, "کلید API", since=m)
    m = tg.mark(); mid = tg.text(BAD, json.dumps({"api_key": "k" * 32, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait(lambda c: c[0] == "deleteMessage" and str(c[1]["message_id"]) == str(mid)); tg.wait_text(BAD, "ذخیره شد", since=m)
    m = tg.mark(); tg.text(BAD, "🎯 تسک جدید"); tg.wait_text(BAD, "لینک", since=m)
    m = tg.mark(); tg.text(BAD, slug); msg = tg.wait_text(BAD, "مرحله را انتخاب", since=m)[1]
    m = tg.mark(); tg.callback(BAD, "st:0"); w = tg.wait_text(BAD, "ولت را انتخاب", since=m)[1]
    m = tg.mark(); tg.callback(BAD, w["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
    tg.wait_text(BAD, "تسک ساخته نشد", since=m, timeout=40)
    assert svc.db.tasks(BAD) == []

    # ---- PAID PUBLIC mint end-to-end: price shown, confirmed, exact value paid on the fork
    P = 10 ** 15
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600)
    c_, pkc = fu.funded_wallet(10 ** 16)
    pslug = "e2e-paid"
    fo.drops[pslug] = {"collection_slug": pslug, "collection_name": "Paid Drop", "chain": "robinhood", "drop_type": "seadrop_v1_erc721",
                       "contract_address": fu.NFT, "total_supply": "51", "max_supply": "1000",
                       "stages": [{"uuid": "s-p", "label": "Public stage", "stage_type": "public_sale", "price": str(P),
                                   "price_currency_address": "0x" + "0" * 40, "max_per_wallet": "2",
                                   "start_time": iso(now - 60), "end_time": iso(now + 3600)}]}
    fo.active_after[pslug] = 0
    fo.mint[pslug] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(P), "chain": "robinhood"})
    add_wallet(tg, U, pkc)
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, f"https://opensea.io/collection/{pslug}/overview")
    msg = tg.wait_text(U, "مرحله را انتخاب کنید", since=m)[1]
    assert "0.001000 ETH" in msg["text"] and "شروع شده" in msg["text"]
    m = tg.mark(); tg.callback(U, "st:0"); w = tg.wait_text(U, "ولت را انتخاب کنید", since=m)[1]
    btn = [b[0]["callback_data"] for b in w["reply_markup"]["inline_keyboard"] if b[0]["text"].lower() == c_.address.lower()][0]
    m = tg.mark(); tg.callback(U, btn)
    rep_ = tg.wait_text(U, "تأیید می‌کنید", since=m, timeout=40)[1]["text"]
    assert "دقیقاً 0.001000 ETH" in rep_ and "allowlist ندارد" in rep_
    b0 = fu.eth(c_.address)
    m = tg.mark(); tg.callback(U, "tok"); tg.wait_text(U, "همین الان شروع می‌شود", since=m)
    tg.wait_text(U, "مینت انجام شد", timeout=150, since=m)
    assert fu.holds(c_.address) == 1 and P <= b0 - fu.eth(c_.address) < P + 10 ** 14
    pt = [t for t in svc.db.tasks(U) if t["slug"] == pslug][0]
    assert pt["kind"] == "public" and pt["price_wei"] == str(P) and pt["max_value_wei"] == str(P)

    # ---- typing while the ✅/✖️ confirm is pending gets a clear answer (live bug: "از منو استفاده کنید")
    fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=5)
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, "🎯 e2e-paid / Public stage (عمومی)\nشروع: x")             # pasted bot text, not a link
    tg.wait_text(U, "این پیام لینک کالکشن نیست", since=m)
    m = tg.mark(); tg.text(U, pslug); tg.wait_text(U, "مرحله را انتخاب کنید", since=m)
    m = tg.mark(); tg.text(U, "2"); tg.wait_text(U, "مرحله را از دکمه", since=m)
    m = tg.mark(); tg.callback(U, "st:0"); tg.wait_text(U, "ولت را انتخاب کنید", since=m)
    m = tg.mark(); tg.callback(U, btn); rep_ = tg.wait_text(U, "تأیید می‌کنید", since=m, timeout=40)[1]["text"]
    assert "سقف هر ولت: 1 از 5" in rep_
    m = tg.mark(); tg.text(U, "2"); tg.wait_text(U, "تسک هنوز ثبت نشده", since=m)
    m = tg.mark(); tg.callback(U, "tno"); tg.wait_text(U, "لغو شد", since=m)
    # ---- wallet already at the on-chain per-wallet limit -> refused BEFORE a task exists (live task #3)
    fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=1)
    n_tasks = len(svc.db.tasks(U))
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, pslug); tg.wait_text(U, "مرحله را انتخاب کنید", since=m)
    m = tg.mark(); tg.callback(U, "st:0"); tg.wait_text(U, "ولت را انتخاب کنید", since=m)
    m = tg.mark(); tg.callback(U, btn); rep_ = tg.wait_text(U, "تسک ساخته نشد", since=m, timeout=40)[1]["text"]
    assert "به سقف مینت این مرحله رسیده (1 از 1)" in rep_
    assert len(svc.db.tasks(U)) == n_tasks
    # ---- group chats are ignored entirely
    m = tg.mark()
    tg._push({"message": {"message_id": 1, "date": int(time.time()), "chat": {"id": -100, "type": "supergroup"},
                          "from": tg.user(U), "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})
    time.sleep(1.0)
    assert not [c for c in tg.calls[m:] if c[0] == "sendMessage" and str(c[1].get("chat_id")) == "-100"]

    # ---- admin
    m = tg.mark(); tg.text(7, "/stats"); tg.wait_text(7, "minted 2", since=m)
    m = tg.mark(); tg.text(U, "/stats"); time.sleep(0.8)
    assert not [c for c in tg.calls[m:] if c[0] == "sendMessage" and str(c[1].get("chat_id")) == str(U)]

    # ---- secrets never leave in bot output, never on disk in plaintext
    out = json.dumps([p for _, p in tg.calls], ensure_ascii=False)
    for s in (pk[2:], pkb[2:], pkc[2:], "k" * 32):
        assert s not in out
    for root, _, files in os.walk(world["cfg"].data_dir):
        for f in files:
            blob = open(os.path.join(root, f), "rb").read()
            for s in (pk[2:], pkb[2:], pkc[2:], "k" * 32):
                assert s.encode() not in blob, f
