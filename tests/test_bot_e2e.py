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


def btn_by(msg, needle):
    for row in msg["reply_markup"]["inline_keyboard"]:
        for b in row:
            if needle in b["text"]:
                return b["callback_data"]
    raise AssertionError(f"no button {needle!r}: " + json.dumps(msg["reply_markup"], ensure_ascii=False))


def wallet_btn(msg, addr):
    return [b[0]["callback_data"] for b in msg["reply_markup"]["inline_keyboard"] if b[0]["text"].lower() == addr.lower()][0]


def stage_btn(msg, label):
    return [b[0]["callback_data"] for b in msg["reply_markup"]["inline_keyboard"] if b[0]["text"].endswith(" " + label)][0]


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
    msg = tg.wait_text(U, "با کدام ولت", since=m)[1]
    assert "OG List" in msg["text"] and "🟢 <b>Public</b>" in msg["text"] and "سقف هر ولت: 1" in msg["text"]
    m = tg.mark(); tg.callback(U, wallet_btn(msg, a.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]
    assert "✅ allowlist هست" in sel["text"] and "Limit 1 per wallet" in sel["text"]
    m = tg.mark(); tg.callback(U, stage_btn(sel, "OG List")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    assert "Limit 1 per wallet" in qm["text"] and "حداکثر قابل مینت برای شما: <b>1</b>" in qm["text"]
    m = tg.mark(); tg.callback(U, btn_by(qm, "حداکثر")); sel = tg.wait_text(U, "انتخاب شما: <b>1 عدد</b>", since=m)[1]
    m = tg.mark(); tg.callback(U, "pn"); tg.wait_text(U, "خلاصهٔ برنامهٔ مینت", since=m)
    m = tg.mark(); tg.callback(U, "pa"); tg.callback(U, "pa"); tg.callback(U, "pa")          # triple tap
    tg.wait_text(U, "مسلح شد", since=m); time.sleep(1.0)
    assert len(svc.db.tasks(U)) == 1, [dict(t) for t in svc.db.tasks(U)]
    # runner picks it up, engine mints at T0 on the fork, user gets notified
    tg.wait_text(U, "مینت انجام شد", timeout=150)
    assert fu.holds(a.address) == 1
    assert svc.db.tasks(U)[0]["status"] == "minted"

    # ---- non-eligible user: the signed stage is shown as not selectable, nothing can be armed for it
    subscribe(tg, BAD); add_wallet(tg, BAD, pkb)
    m = tg.mark(); tg.text(BAD, "🔑 کلید API"); tg.wait_text(BAD, "ثبت نشده", since=m)
    m = tg.mark(); tg.callback(BAD, "kset"); tg.wait_text(BAD, "کلید API", since=m)
    m = tg.mark(); mid = tg.text(BAD, json.dumps({"api_key": "k" * 32, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait(lambda c: c[0] == "deleteMessage" and str(c[1]["message_id"]) == str(mid)); tg.wait_text(BAD, "ذخیره شد", since=m)
    m = tg.mark(); tg.text(BAD, "🎯 تسک جدید"); tg.wait_text(BAD, "لینک", since=m)
    m = tg.mark(); tg.text(BAD, slug); msg = tg.wait_text(BAD, "با کدام ولت", since=m)[1]
    m = tg.mark(); tg.callback(BAD, wallet_btn(msg, b.address))
    sel = tg.wait_text(BAD, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]
    assert "⛔ <b>OG List</b>" in sel["text"] and "در allowlist این مرحله نیست" in sel["text"]
    assert not any("OG List" in r[0]["text"] for r in sel["reply_markup"]["inline_keyboard"])
    m = tg.mark(); tg.callback(BAD, "ps:0"); tg.wait_text(BAD, "جلسه منقضی شد", since=m)       # forged callback refused
    m = tg.mark(); tg.callback(BAD, "px"); tg.wait_text(BAD, "لغو شد", since=m)
    assert svc.db.tasks(BAD) == []

    # ---- MULTI-STAGE PLAN with quantities on ONE wallet: Allowlist (signed, free) x2 + Public (paid) x3
    P = 10 ** 15
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=10)
    c_, pkc = fu.funded_wallet(10 ** 16)
    pslug = "e2e-paid"
    fo.drops[pslug] = {"collection_slug": pslug, "collection_name": "Paid Drop", "chain": "robinhood", "drop_type": "seadrop_v1_erc721",
                       "contract_address": fu.NFT, "total_supply": "51", "max_supply": "1000",
                       "stages": [{"uuid": "s-al", "label": "Allowlist", "stage_type": "signed_presale", "price": "0",
                                   "price_currency_address": "0x" + "0" * 40, "max_per_wallet": "10",
                                   "start_time": iso(now - 60), "end_time": iso(now + 3600)},
                                  {"uuid": "s-p", "label": "Public stage", "stage_type": "public_sale", "price": str(P),
                                   "price_currency_address": "0x" + "0" * 40, "max_per_wallet": "10",
                                   "start_time": iso(now - 30), "end_time": iso(now + 3600)}]}
    fo.eligible.add(c_.address.lower())

    # OpenSea serves per request; the fake routes by quantity (2 -> allowlist signature, 3 -> public calldata)
    fo.mint[pslug] = lambda m, q: ((200, {"to": fu.SD, "data": fu.signed_calldata(world["ts"], c_.address, 0xA11, q=q, max_per_wallet=10,
                                                                                    start=now - 60, end=now + 3600),
                                          "value": "0", "chain": "robinhood"}) if q == 2 else
                                   (200, {"to": fu.SD, "data": fu.public_calldata(m, q), "value": str(P * q), "chain": "robinhood"}))
    add_wallet(tg, U, pkc)
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, "check this https://opensea.io/collection/e2e-paid/overview please")   # link inside text
    msg = tg.wait_text(U, "با کدام ولت", since=m)[1]
    assert "0.001000 ETH" in msg["text"] and "شروع شده" in msg["text"]
    m = tg.mark(); tg.text(U, "2"); tg.wait_text(U, "ولت را از دکمه", since=m)                    # typed while choosing
    m = tg.mark(); tg.callback(U, wallet_btn(msg, c_.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]
    assert "تجمعی" in sel["text"] and "Limit 10 per wallet" in sel["text"]
    # Allowlist: custom amount 2 (Persian digits)
    m = tg.mark(); tg.callback(U, stage_btn(sel, "Allowlist")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    m = tg.mark(); tg.text(U, "hello"); tg.wait_text(U, "تعداد را با دکمه", since=m)
    m = tg.mark(); tg.callback(U, btn_by(qm, "عدد دلخواه")); tg.wait_text(U, "عدد مورد نظر را بفرستید", since=m)
    m = tg.mark(); tg.text(U, "99"); tg.wait_text(U, "یک عدد بین ۱ و 10", since=m)              # above max -> refused
    m = tg.mark(); tg.text(U, "۲"); sel = tg.wait_text(U, "انتخاب شما: <b>2 عدد</b>", since=m)[1]
    # Public: preset 3
    m = tg.mark(); tg.callback(U, stage_btn(sel, "Public stage")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    assert "قیمت هر عدد: 0.001000 ETH" in qm["text"]
    m = tg.mark(); tg.callback(U, btn_by(qm, "3")); sel = tg.wait_text(U, "انتخاب شما: <b>3 عدد</b>", since=m)[1]
    m = tg.mark(); tg.callback(U, "pn"); summ = tg.wait_text(U, "خلاصهٔ برنامهٔ مینت", since=m)[1]["text"]
    assert "0.001000 × 3 = 0.003000 ETH" in summ and "تعداد: <b>2</b>" in summ and "رایگان" in summ
    m = tg.mark(); tg.text(U, "ok"); tg.wait_text(U, "برنامه هنوز ثبت نشده", since=m)
    b0 = fu.eth(c_.address)
    m = tg.mark(); tg.callback(U, "pa"); arm = tg.wait_text(U, "مسلح شد", since=m)[1]["text"]
    assert "Allowlist × 2" in arm and "Public stage × 3" in arm
    tg.wait_text(U, "مینت انجام شد — 2 عدد", timeout=150, since=m)
    tg.wait_text(U, "مینت انجام شد — 3 عدد", timeout=150, since=m)
    assert fu.holds(c_.address) == 5 and 3 * P <= b0 - fu.eth(c_.address) < 3 * P + 2 * 10 ** 14
    pts = sorted([t for t in svc.db.tasks(U) if t["slug"] == pslug], key=lambda t: t["id"])
    assert [(t["stage_label"], t["quantity"], t["kind"], t["status"]) for t in pts] == \
        [("Allowlist", 2, "signed", "minted"), ("Public stage", 3, "public", "minted")]
    assert pts[1]["price_wei"] == str(P) and pts[1]["max_value_wei"] == str(3 * P) and pts[0]["plan_id"] == pts[1]["plan_id"]

    # ---- cumulative wallet limit: the wallet holds 5 now; with limit 5 everything is shown as full, nothing armable
    fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=5)
    fo.drops[pslug]["stages"] = [fo.drops[pslug]["stages"][1]]
    n_tasks = len(svc.db.tasks(U))
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, pslug); msg = tg.wait_text(U, "با کدام ولت", since=m)[1]
    m = tg.mark(); tg.callback(U, wallet_btn(msg, c_.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]["text"]
    assert "سقف ولت پر است (5 از 5)" in sel
    assert len(svc.db.tasks(U)) == n_tasks

    # ---- group chats are ignored entirely
    m = tg.mark()
    tg._push({"message": {"message_id": 1, "date": int(time.time()), "chat": {"id": -100, "type": "supergroup"},
                          "from": tg.user(U), "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})
    time.sleep(1.0)
    assert not [c for c in tg.calls[m:] if c[0] == "sendMessage" and str(c[1].get("chat_id")) == "-100"]

    # ---- admin
    m = tg.mark(); tg.text(7, "/stats"); tg.wait_text(7, "minted 3", since=m)
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
