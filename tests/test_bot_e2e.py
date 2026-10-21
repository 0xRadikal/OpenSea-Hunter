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
    short = f"{addr[:6]}…{addr[-4:]}"
    return [b[0]["callback_data"] for b in msg["reply_markup"]["inline_keyboard"]
            if b[0].get("callback_data", "").startswith("pw:") and b[0]["text"] == short][0]


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
    assert "🟢 OG List | Allowlist (امضادار) | رایگان | 1 |" in msg["text"]
    assert "🟢 Public | Public (عمومی) | 0.100000 ETH | 5 |" in msg["text"]
    assert "\u2066OG List\u2069" in msg["_rich"]["html"] and msg["_rich"]["is_rtl"] is True       # rich RTL + isolated Latin
    m = tg.mark(); tg.callback(U, wallet_btn(msg, a.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]
    assert "| ✅ هست | Limit 1 | حداکثر 1 | —" in sel["text"] and "\u2066Limit 1\u2069" in sel["_rich"]["html"]
    m = tg.mark(); tg.callback(U, stage_btn(sel, "OG List")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    assert "سقف هر ولت در این مرحله | Limit 1 per wallet" in qm["text"] and "حداکثر قابل مینت برای شما در این مرحله | 1" in qm["text"]
    m = tg.mark(); tg.callback(U, btn_by(qm, "حداکثر")); sel = tg.wait_text(U, "| Limit 1 | حداکثر 1 | 1", since=m)[1]
    m = tg.mark(); tg.callback(U, "pn"); tg.wait_text(U, "خلاصهٔ برنامهٔ مینت", since=m)
    m = tg.mark(); tg.callback(U, "pa"); tg.callback(U, "pa"); tg.callback(U, "pa")          # triple tap
    tg.wait_text(U, "مسلح شد", since=m); time.sleep(1.0)
    assert len(svc.db.tasks(U)) == 1, [dict(t) for t in svc.db.tasks(U)]
    # runner picks it up, engine mints at T0 on the fork, user gets notified
    tg.wait_text(U, "مینت موفق! #1", timeout=150)
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
    assert "⛔ OG List" in sel["text"] and "| طبق OpenSea این ولت در allowlist این مرحله نیست |" in sel["text"]
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
    # second API key -> each concurrent stage gets its OWN key; same key twice is deduplicated
    fo.valid_keys.add("q" * 32)
    m = tg.mark(); tg.webapp(U, json.dumps({"api_key": "q" * 32, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait_text(U, "کلیدهای معتبر شما | 2", since=m)
    m = tg.mark(); tg.webapp(U, json.dumps({"api_key": "q" * 32, "expires_at": iso(time.time() + 7 * 86400)}))
    tg.wait_text(U, "قبلاً ثبت شده", since=m)
    m = tg.mark(); tg.text(U, "🔑 کلید API"); km = tg.wait_text(U, "کلیدهای API شخصی", since=m)[1]["text"]
    assert km.count("🟢") == 2
    # settings: price policy -> cap +25% (only affects plans armed from now on)
    m = tg.mark(); tg.text(U, "⚙️ تنظیمات"); st_ = tg.wait_text(U, "واکنش به تغییر قیمت", since=m)[1]
    assert "فقط با همان قیمت" in st_["text"] and "سقف تسک فعال | 20" in st_["text"]
    m = tg.mark(); tg.callback(U, "cp:cap"); st_ = tg.wait_text(U, "تا سقف افزایش مشخص", since=m)[1]
    m = tg.mark(); tg.callback(U, "cpp:25"); tg.wait_text(U, "سقف +25٪", since=m)
    assert svc.db.prefs(U) == {"user_id": U, "price_policy": "cap", "max_increase_pct": 25}
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, "check this https://opensea.io/collection/e2e-paid/overview please")   # link inside text
    msg = tg.wait_text(U, "با کدام ولت", since=m)[1]
    assert "0.001000 ETH" in msg["text"] and "شروع شده" in msg["text"]
    m = tg.mark(); tg.text(U, "2"); tg.wait_text(U, "ولت را از دکمه", since=m)                    # typed while choosing
    m = tg.mark(); tg.callback(U, wallet_btn(msg, c_.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]
    assert "جدا برای هر مرحله" in sel["text"] and "تجمعی" not in sel["text"] and "| Limit 10 | حداکثر 10 | —" in sel["text"]
    # Allowlist: custom amount 2 (Persian digits)
    m = tg.mark(); tg.callback(U, stage_btn(sel, "Allowlist")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    m = tg.mark(); tg.text(U, "hello"); tg.wait_text(U, "تعداد را با دکمه", since=m)
    m = tg.mark(); tg.callback(U, btn_by(qm, "عدد دلخواه")); tg.wait_text(U, "عدد مورد نظر را بفرستید", since=m)
    m = tg.mark(); tg.text(U, "99"); tg.wait_text(U, "یک عدد بین 1 و 10", since=m)              # above max -> refused
    m = tg.mark(); tg.text(U, "۲"); sel = tg.wait_text(U, "| Limit 10 | حداکثر 10 | 2", since=m)[1]
    # Public: preset 3
    m = tg.mark(); tg.callback(U, stage_btn(sel, "Public stage")); qm = tg.wait_text(U, "چند عدد مینت کنید", since=m)[1]
    assert "قیمت هر عدد | 0.001000 ETH" in qm["text"]
    m = tg.mark(); tg.callback(U, btn_by(qm, "3")); sel = tg.wait_text(U, "| حداکثر 9 | 3", since=m)[1]
    m = tg.mark(); tg.callback(U, "pn"); summ_p = tg.wait_text(U, "خلاصهٔ برنامهٔ مینت", since=m)[1]; summ = summ_p["text"]
    assert "| 3 | 0.001000 × 3 = 0.003000 ETH" in summ and "| 2 | رایگان" in summ
    assert "قیمت قابل‌قبول Public stage | 0.001000 – 0.001250 ETH" in summ and "سقف +25٪" in summ
    assert "<tg-math-block>0.00125 \\times 3 = 0.00375\\ \\mathrm{ETH}</tg-math-block>" in summ_p["_rich"]["html"]
    assert "کلید هر مرحله" in summ and "کلید مشترک" not in summ
    m = tg.mark(); tg.text(U, "ok"); tg.wait_text(U, "برنامه هنوز ثبت نشده", since=m)
    b0 = fu.eth(c_.address)
    m = tg.mark(); tg.callback(U, "pa"); arm = tg.wait_text(U, "مسلح شد", since=m)[1]["text"]
    assert "| Allowlist | 2 |" in arm and "| Public stage | 3 |" in arm
    done2 = tg.wait_text(U, "مینت انجام شد — 2 عدد", timeout=150, since=m)[1]
    done3 = tg.wait_text(U, "مینت انجام شد — 3 عدد", timeout=150, since=m)[1]
    for c_msg in (done2, done3):
        d = c_msg["text"]
        assert c_msg["_rich"]["is_rtl"] is True and c_msg.get("message_effect_id")
        assert "⚡ سرعت ربات" in d and "ارسال تراکنش (پذیرش sequencer) |" in d and "تعداد |" in d, d
        assert "معیار رقابت نیست" in d           # stage started ~1 min before arming: reported as late, not a record
    assert "قیمت پرداختی هر عدد | 0.001000 ETH" in done3["text"] and "تعداد | 3" in done3["text"]
    assert fu.holds(c_.address) == 5 and 3 * P <= b0 - fu.eth(c_.address) < 3 * P + 2 * 10 ** 14
    pts = sorted([t for t in svc.db.tasks(U) if t["slug"] == pslug], key=lambda t: t["id"])
    assert [(t["stage_label"], t["quantity"], t["kind"], t["status"]) for t in pts] == \
        [("Allowlist", 2, "signed", "minted"), ("Public stage", 3, "public", "minted")]
    assert pts[1]["price_wei"] == str(P) and pts[1]["max_value_wei"] == str(3 * P * 5 // 4) and pts[0]["plan_id"] == pts[1]["plan_id"]
    assert pts[1]["price_policy"] == "cap" and pts[1]["max_unit_wei"] == str(P * 5 // 4) and pts[1]["min_unit_wei"] == str(P)
    assert pts[0]["apikey_id"] != pts[1]["apikey_id"] and None not in (pts[0]["apikey_id"], pts[1]["apikey_id"])
    used = {(b["quantity"], b["_key"]) for b in fo.mint_bodies if b["_slug"] == pslug}
    assert {k for q_, k in used if q_ == 2} and {k for q_, k in used if q_ == 3}
    assert not ({k for q_, k in used if q_ == 2} & {k for q_, k in used if q_ == 3}), used   # really different keys

    # ---- per-stage wallet limit: the wallet minted 5; OpenSea eligibility caps this stage at 5 -> full, nothing armable
    fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=5)
    fo.caps[(pslug, "s-p")] = 5
    fo.drops[pslug]["stages"] = [fo.drops[pslug]["stages"][1]]
    n_tasks = len(svc.db.tasks(U))
    m = tg.mark(); tg.text(U, "🎯 تسک جدید"); tg.wait_text(U, "لینک کالکشن", since=m)
    m = tg.mark(); tg.text(U, pslug); msg = tg.wait_text(U, "با کدام ولت", since=m)[1]
    m = tg.mark(); tg.callback(U, wallet_btn(msg, c_.address))
    sel = tg.wait_text(U, "مرحله(ها) را انتخاب", since=m, timeout=40)[1]["text"]
    assert "سقف این مرحله برای این ولت پر است (Limit 10 per wallet)" in sel, sel
    assert len(svc.db.tasks(U)) == n_tasks

    # ---- a Bot API server without Rich Messages: every screen still arrives as classic HTML (fallback)
    tg.reject_rich = True
    try:
        m = tg.mark(); tg.text(U, "⚙️ تنظیمات")
        c_ = tg.wait(lambda c: c[0] == "sendMessage" and str(c[1].get("chat_id")) == str(U) and "واکنش به تغییر قیمت" in c[1].get("text", ""),
                     since=m)[1]
        assert any(x[0] == "sendRichMessage:rejected" for x in tg.calls[m:]) and c_["parse_mode"] == "HTML"
        assert "<table" not in c_.get("_raw_text", c_["text"]) and "سقف تسک فعال:" in c_["text"]
    finally:
        tg.reject_rich = False

    # ---- group chats are ignored entirely
    m = tg.mark()
    tg._push({"message": {"message_id": 1, "date": int(time.time()), "chat": {"id": -100, "type": "supergroup"},
                          "from": tg.user(U), "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})
    time.sleep(1.0)
    assert not [c for c in tg.calls[m:] if c[0] in ("sendMessage", "sendRichMessage") and str(c[1].get("chat_id")) == "-100"]

    # ---- admin
    m = tg.mark(); tg.text(U, "/setmax 50"); time.sleep(0.8)                                  # non-admin ignored
    assert svc.db.max_active(20) == 20
    m = tg.mark(); tg.text(7, "/setmax 50"); tg.wait_text(7, "max active tasks = 50", since=m)
    assert svc.max_active() == 50
    m = tg.mark(); tg.text(7, "/stats"); tg.wait_text(7, "minted 3", since=m)
    m = tg.mark(); tg.text(U, "/stats"); time.sleep(0.8)
    assert not [c for c in tg.calls[m:] if c[0] in ("sendMessage", "sendRichMessage") and str(c[1].get("chat_id")) == str(U)]

    # ---- secrets never leave in bot output, never on disk in plaintext
    out = json.dumps([p for _, p in tg.calls], ensure_ascii=False)
    for s in (pk[2:], pkb[2:], pkc[2:], "k" * 32):
        assert s not in out
    for root, _, files in os.walk(world["cfg"].data_dir):
        for f in files:
            blob = open(os.path.join(root, f), "rb").read()
            for s in (pk[2:], pkb[2:], pkc[2:], "k" * 32):
                assert s.encode() not in blob, f
