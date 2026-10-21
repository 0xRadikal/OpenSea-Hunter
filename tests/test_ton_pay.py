"""TON gateway: address codec, parsing of REAL TonAPI transactions, matching rules, idempotency, partial/over payment,
cursor handling and outages — against a fake TonAPI that mirrors the live API semantics."""
import asyncio
import json
import os
import time

import pytest

from service import ton_pay as T
from service.config import Config
from service.db import DB
from tests.fake_tonapi import FakeTonApi

ADDR = "UQDz3fDX55jDBLWnhRKX2K7KppTtm2YmCrEdxwrWcb-E32rx"
RAW = "0:f3ddf0d7e798c304b5a7851297d8aecaa694ed9b66260ab11dc70ad671bf84df"
FIX = os.path.join(os.path.dirname(__file__), "fixture_tonapi_txs.json")
P = 5 * T.NANO


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ pure functions
def test_address_codec_matches_tonapi_parse():
    # values from GET /v2/address/{raw}/parse (live, 2026-10-21)
    assert T.raw_address(ADDR) == RAW
    assert T.friendly_address(RAW, bounceable=True) == "EQDz3fDX55jDBLWnhRKX2K7KppTtm2YmCrEdxwrWcb-E3zc0"
    assert T.friendly_address(RAW) == ADDR
    assert T.raw_address("EQDz3fDX55jDBLWnhRKX2K7KppTtm2YmCrEdxwrWcb+E3zc0") == RAW       # standard base64 too
    assert T.parse_address(ADDR)[2] == {"form": "friendly", "bounceable": False, "testnet": False}
    assert ADDR[-1] == "x"
    for bad in ("", ADDR[:-1] + "y", ADDR[:10] + "B" + ADDR[11:], ADDR[:-2], "0:zz", "UQ" + "A" * 46):
        with pytest.raises(ValueError):
            T.parse_address(bad)


def test_amount_and_memo_helpers():
    assert T.fmt_ton(5_000_100_000) == "5.0001" and T.fmt_ton(T.NANO) == "1.0" and T.fmt_ton(1) == "0.000000001"
    assert T.parse_ton("۵٫۲") == 5_200_000_000 and T.parse_ton("0,5") == 500_000_000 and T.parse_ton(" 3 ") == 3 * T.NANO
    for bad in ("0", "-1", "1e3", "abc", "1.0000000001", ""):
        with pytest.raises(ValueError):
            T.parse_ton(bad)
    m = T.new_memo()
    assert T.MEMO_RE.fullmatch(m) and T.extract_memo(f"  {m.lower()[:4]}-{m.lower()[4:]} thanks") == m
    assert T.extract_memo("RM0OIL1234") is None and T.extract_memo(None) is None and T.extract_memo("hello") is None


def test_links_and_qr():
    L = T.transfer_links(ADDR, 5_000_100_000, "RMABCDEFGH")
    assert L["ton"] == f"ton://transfer/{ADDR}?amount=5000100000&text=RMABCDEFGH"
    assert L["tonkeeper"] == f"https://app.tonkeeper.com/transfer/{ADDR}?amount=5000100000&text=RMABCDEFGH"
    png = T.qr_png(L["ton"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 300


def test_incoming_on_real_chain_transactions():
    """Recorded from TonAPI for the real receive address: only successful inbound internal TON transfers count."""
    d = json.load(open(FIX))
    txs = d["transactions"]
    got = [T.incoming(t, RAW) for t in txs]
    kinds = [((t.get("in_msg") or {}).get("msg_type"), (t.get("in_msg") or {}).get("decoded_op_name"), t["success"]) for t in txs]
    for k, g in zip(kinds, got):
        if k[0] != "int_msg" or not k[2]:
            assert g is None, k                 # outgoing (ext_in) and failed (nft_ownership_assigned, success=False)
        else:
            assert g and g["value"] > 0, k
    comments = [g for g in got if g and g["comment"]]
    assert len(comments) == 3 and all(g["memo"] is None for g in comments)    # spam comments are NOT invoices
    assert all(T.incoming(t, "0:" + "11" * 32) is None for t in txs)           # other account -> never


def mk(value, **kw):
    im = {"msg_type": "int_msg", "value": value, "bounced": False, "destination": {"address": RAW},
          "source": {"address": "0:" + "ab" * 32}}
    im.update(kw.pop("im", {}))
    t = {"hash": "aa" * 32, "lt": 5, "utime": 100, "success": True, "aborted": False, "transaction_type": "TransOrd",
         "account": {"address": RAW}, "in_msg": im}
    t.update(kw)
    return t


@pytest.mark.parametrize("tx", [
    mk(0), mk(-5), mk(P, success=False), mk(P, aborted=True), mk(P, transaction_type="TransTickTock"),
    mk(P, im={"bounced": True}), mk(P, im={"msg_type": "ext_in_msg"}), mk(P, im={"destination": {"address": "0:" + "22" * 32}}),
    mk(P, account={"address": "0:" + "22" * 32}), mk(P, in_msg=None), mk(P, im={"value": "x"})])
def test_incoming_rejects(tx):
    assert T.incoming(tx, RAW) is None


def test_incoming_accepts_and_reads_memo():
    g = T.incoming(mk(P, im={"decoded_op_name": "text_comment", "decoded_body": {"text": "rmabcdefgh"}}), RAW)
    assert g["value"] == P and g["memo"] == "RMABCDEFGH" and g["hash"] == "aa" * 32
    assert T.incoming(mk(P), RAW)["memo"] is None


# ------------------------------------------------------------------ gateway against fake TonAPI
@pytest.fixture
def gw(tmp_path):
    api = FakeTonApi(RAW)
    cfg = Config()
    cfg.ton_address, cfg.ton_api_key, cfg.ton_api_base, cfg.price_ton, cfg.sub_days = ADDR, "testkey", api.url, "5", 30
    db = DB(str(tmp_path / "t.db"))
    ev = []
    g = T.TonGateway(db, cfg, on_event=lambda k, d: ev.append((k, d)))
    g.ev, g.fake, g.db = ev, api, db
    yield g
    api.close()


def boot(g):
    """First poll sets the cursor to the newest existing tx (old history is never credited)."""
    g.fake.pay(3 * T.NANO, "old")
    run(g.poll_once())
    assert g.db.setting("ton_last_lt") is not None and g.ev == []


def test_enabled_and_price(gw):
    assert gw.enabled() and gw.price_nano() == P
    gw.db.set_setting("ton_price_nano", 0)
    assert not gw.enabled()
    gw.db.set_setting("ton_price_nano", 7 * T.NANO)
    assert gw.price_nano() == 7 * T.NANO


def test_invalid_or_testnet_address_disables(tmp_path):
    for a in ("garbage", "kQDz3fDX55jDBLWnhRKX2K7KppTtm2YmCrEdxwrWcb-E3yxr"):
        cfg = Config(); cfg.ton_address, cfg.ton_api_key, cfg.price_ton = a, "k", "5"
        g = T.TonGateway(DB(str(tmp_path / f"{len(a)}.db")), cfg)
        assert g.raw is None and not g.enabled()


def test_invoice_unique_amounts_and_reuse(gw):
    now = int(time.time())
    a = gw.invoice_for(1, now)
    assert gw.invoice_for(1, now + 10)["id"] == a["id"]                       # reused while valid
    amts = {gw.invoice_for(u, now)["amount_nano"] for u in range(2, 60)} | {a["amount_nano"]}
    assert len(amts) == 59                                                      # all distinct
    assert all(P < x < P + 10 ** 8 and (x - P) % T.OFFSET_STEP == 0 for x in amts)
    assert gw.invoice_for(1, now + T.INVOICE_TTL)["id"] != a["id"]              # expired -> new invoice


def test_memo_payment_credits_once(gw):
    boot(gw)
    inv = gw.invoice_for(10)
    h = gw.fake.pay(inv["amount_nano"], f"payment {inv['memo']}")
    assert run(gw.poll_once()) == 1
    u = gw.db.user(10)
    assert u["sub_until"] > time.time() + 29 * 86400
    k, d = gw.ev[-1]
    assert k == "paid" and d["uid"] == 10 and d["how"] == "memo" and d["inc"]["hash"] == h
    # replay: cursor reset to before the tx -> tx seen again, never credited twice
    gw.db.set_setting("ton_last_lt", 0)
    assert run(gw.poll_once()) == 0 and gw.db.user(10)["sub_until"] == u["sub_until"]
    assert gw.db.ton_invoice(inv["id"])["status"] == "paid"


def test_amount_only_payment_and_ambiguity(gw):
    boot(gw)
    inv = gw.invoice_for(11)
    gw.fake.pay(inv["amount_nano"])                                             # exchange without comment
    run(gw.poll_once())
    assert gw.ev[-1][0] == "paid" and gw.ev[-1][1]["how"] == "amount" and gw.db.is_subscribed(11)
    gw.fake.pay(P)                                                              # round price, no memo: matches nobody
    run(gw.poll_once())
    assert gw.ev[-1][0] == "unmatched"
    inv2 = gw.invoice_for(12)
    gw.fake.pay(inv2["amount_nano"], "RMZZZZZZZZ")                              # unknown memo -> falls back to unique amount
    run(gw.poll_once())
    assert gw.ev[-1][0] == "paid" and gw.ev[-1][1]["uid"] == 12


def test_amount_match_respects_expiry(gw):
    boot(gw)
    inv = gw.invoice_for(13)
    gw.fake.pay(inv["amount_nano"], utime=inv["expires"] + 60)                  # sent after the unique-amount window
    run(gw.poll_once())
    assert gw.ev[-1][0] == "unmatched" and not gw.db.is_subscribed(13)


def test_memo_still_counts_after_expiry(gw):
    boot(gw)
    inv = gw.invoice_for(14)
    gw.fake.pay(inv["amount_nano"], inv["memo"], utime=inv["expires"] + 7200)
    run(gw.poll_once())
    assert gw.ev[-1][0] == "paid" and gw.db.is_subscribed(14)


def test_partial_then_rest_and_overpay_periods(gw):
    boot(gw)
    inv = gw.invoice_for(15)
    gw.fake.pay(2 * T.NANO, inv["memo"])
    run(gw.poll_once())
    k, d = gw.ev[-1]
    assert k == "partial" and d["need"] == 3 * T.NANO and not gw.db.is_subscribed(15)
    gw.fake.pay(3 * T.NANO, inv["memo"])
    run(gw.poll_once())
    assert gw.ev[-1][0] == "paid" and gw.ev[-1][1]["periods"] == 1
    s1 = gw.db.user(15)["sub_until"]
    gw.fake.pay(11 * T.NANO, inv["memo"])                                       # 16 total = 3 periods (+1 TON credit)
    run(gw.poll_once())
    k, d = gw.ev[-1]
    assert k == "paid" and d["periods"] == 2 and gw.db.user(15)["sub_until"] - s1 == 60 * 86400
    assert gw.db.ton_invoice(inv["id"])["paid_nano"] == 16 * T.NANO


def test_rejected_transfers_are_ignored(gw):
    boot(gw)
    inv = gw.invoice_for(16)
    gw.fake.pay(inv["amount_nano"], inv["memo"], success=False)
    gw.fake.pay(inv["amount_nano"], inv["memo"], bounced=True)
    gw.fake.pay(inv["amount_nano"], inv["memo"], aborted=True)
    gw.fake.pay(inv["amount_nano"], inv["memo"], msg_type="ext_in_msg")
    run(gw.poll_once())
    assert gw.ev == [] and not gw.db.is_subscribed(16)


def test_old_history_never_credited_on_first_run(gw):
    inv = gw.invoice_for(17, now=int(time.time()))
    gw.fake.pay(inv["amount_nano"], inv["memo"], utime=int(time.time()) - 7200)  # before the invoice existed
    gw.fake.pay(inv["amount_nano"], inv["memo"])                                  # after: valid
    run(gw.poll_once())
    assert [k for k, _ in gw.ev] == ["paid"] and gw.db.ton_invoice(inv["id"])["paid_nano"] == inv["amount_nano"]


def test_outage_keeps_cursor_and_recovers(gw):
    boot(gw)
    inv = gw.invoice_for(18)
    cur = gw.db.setting("ton_last_lt")
    gw.fake.pay(inv["amount_nano"], inv["memo"])
    gw.fake.fail = 10                                                           # > retries
    assert run(gw.poll_once()) == 0 and gw.healthy is False and gw.db.setting("ton_last_lt") == cur
    gw.fake.fail = 0
    assert run(gw.poll_once()) == 1 and gw.healthy is True and gw.db.is_subscribed(18)


def test_transient_error_retried(gw):
    boot(gw)
    inv = gw.invoice_for(19)
    gw.fake.pay(inv["amount_nano"], inv["memo"])
    gw.fake.fail = 2                                                            # < retries
    assert run(gw.poll_once()) == 1


def test_bad_key_is_not_retried(gw):
    gw.api.h = {"Authorization": "Bearer wrong"}
    n0 = len(gw.fake.calls)
    assert run(gw.poll_once()) == 0 and gw.healthy is False and len(gw.fake.calls) - n0 == 1


def test_pagination_over_many_transactions(gw):
    boot(gw)
    inv = gw.invoice_for(20)
    for _ in range(230):
        gw.fake.pay(1, "spam")
    gw.fake.pay(inv["amount_nano"], inv["memo"])
    run(gw.poll_once())
    assert gw.db.is_subscribed(20) and int(gw.db.setting("ton_last_lt")) == gw.fake.lt


def test_concurrent_polls_do_not_double_credit(gw):
    boot(gw)
    inv = gw.invoice_for(21)
    gw.fake.pay(inv["amount_nano"], inv["memo"])

    async def both():
        return await asyncio.gather(gw.poll_once(), gw.poll_once(), gw.poll_once())
    assert sum(run(both())) == 1
    assert gw.db.q("SELECT COUNT(*) FROM ton_payments WHERE kind='paid'")[0][0] == 1


def test_admin_assign_unmatched_once(gw):
    boot(gw)
    h = gw.fake.pay(4 * T.NANO, "no code")
    run(gw.poll_once())
    assert gw.ev[-1][0] == "unmatched"
    until = gw.db.ton_assign(h, 22, 30, int(time.time()), 7)
    assert until and gw.db.is_subscribed(22)
    assert gw.db.ton_assign(h, 23, 30, int(time.time()), 7) is None             # cannot be reused
    assert gw.db.ton_assign("ff" * 32, 23, 30, int(time.time()), 7) is None


def test_rollback_on_failure_leaves_nothing(gw, monkeypatch):
    boot(gw)
    inv = gw.invoice_for(24)
    gw.fake.pay(inv["amount_nano"], inv["memo"])
    real = gw.db.extend_sub

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("disk full")
    monkeypatch.setattr(gw.db, "extend_sub", boom)
    with pytest.raises(RuntimeError):
        run(gw.poll_once())
    monkeypatch.setattr(gw.db, "extend_sub", real)
    assert not gw.db.is_subscribed(24) and gw.db.q("SELECT COUNT(*) FROM ton_payments WHERE kind!='history'")[0][0] == 0
    assert gw.db.ton_invoice(inv["id"])["paid_nano"] == 0
    run(gw.poll_once())                                                         # retried after the failure
    assert gw.db.is_subscribed(24)


def test_encrypted_comment_from_real_payment_uses_unique_amount(gw):
    """The admin's real test payment (2026-10-21, Tonkeeper) carried an ENCRYPTED comment (op 0x2167da4b);
    it must be treated as 'no memo' and matched by the unique amount."""
    boot(gw)
    inv = gw.invoice_for(30)
    gw.fake.pay(inv["amount_nano"])
    t = gw.fake.txs[-1]["in_msg"]
    t.update({"op_code": "0x2167da4b", "decoded_op_name": "encrypted_text_comment",
              "decoded_body": {"cipher_text": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACJIePiGgYKus85THBEmHx5hqc2dvw/TYyDY4uk6qVf"}})
    inc = T.incoming(gw.fake.txs[-1], RAW)
    assert inc["encrypted"] is True and inc["memo"] is None and inc["comment"] is None
    run(gw.poll_once())
    k, d = gw.ev[-1]
    assert k == "paid" and d["how"] == "amount" and d["inc"]["encrypted"] is True and gw.db.is_subscribed(30)


def test_api_outage_alert_once_and_recovery(gw, monkeypatch):
    boot(gw)
    clock = [time.time()]
    monkeypatch.setattr(T.time, "time", lambda: clock[0])
    gw.fake.fail = 10 ** 6
    run(gw.poll_once())                                               # first failure: no alert yet
    assert not [k for k, _ in gw.ev if k.startswith("api_")]
    clock[0] += gw.OUTAGE_ALERT_S + 1
    run(gw.poll_once()); run(gw.poll_once())
    assert [k for k, _ in gw.ev].count("api_down") == 1             # exactly once
    gw.fake.fail = 0
    clock[0] += 60
    run(gw.poll_once()); run(gw.poll_once())
    assert [k for k, _ in gw.ev].count("api_up") == 1 and gw.ev[-1][1]["secs"] > 0 if gw.ev[-1][0] == "api_up" else True
    assert [k for k, _ in gw.ev][-1] == "api_up"


def test_short_blip_does_not_alert(gw):
    boot(gw)
    gw.fake.fail = 10
    run(gw.poll_once())
    gw.fake.fail = 0
    run(gw.poll_once())
    assert not [k for k, _ in gw.ev if k.startswith("api_")]
