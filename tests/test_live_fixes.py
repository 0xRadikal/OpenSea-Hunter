"""Unit tests for the 2026-10-20 live fixes: result classification, link extraction, on-chain limit helpers."""
import pytest
from service import preflight as pf
from service.bot import Service
from service.runner import classify


def test_classify_minted_even_if_holds_lags():
    st, msg = classify({"minted": True, "tx": "0xabc", "token_ids": [42], "reason": None, "error": "", "holds": 0})
    assert st == "minted" and "#42" in msg and "0xabc" in msg


@pytest.mark.parametrize("reason,status,needle", [
    ("wallet_limit", "failed", "سقف"), ("not_eligible", "not_eligible", "allowlist"),
    ("rpc_unavailable", "failed", "اتصال"), ("bad_input", "failed", "ادمین")])
def test_classify_reasons(reason, status, needle):
    st, msg = classify({"minted": False, "reason": reason, "error": ""})
    assert st == status and needle in msg


def test_classify_never_leaks_raw_errors():
    st, msg = classify({"minted": False, "reason": "error", "error": "KeyError: 'result'"}, "Traceback ... KeyError")
    assert st == "failed" and "KeyError" not in msg and "Traceback" not in msg
    assert classify(None)[0] == "failed"
    assert classify({"minted": False, "reason": "not_minted", "error": ""}, "tx REVERTED status 0")[1] == "تراکنش روی زنجیره رد شد"


@pytest.mark.parametrize("text,want", [
    ("aerent", "aerent"),
    ("https://opensea.io/collection/an-asshole/overview", "https://opensea.io/collection/an-asshole/overview"),
    ("check this out https://opensea.io/collection/an-asshole now", "https://opensea.io/collection/an-asshole"),
    ("🎯 an-asshole / Public stage (عمومی)\nشروع: 2026", None),                # pasted bot text
    ("https://opensea.io/collection/a and https://opensea.io/collection/b", None),
    ("", None), ("   ", None)])
def test_extract_link(text, want):
    assert Service.extract_link(text) == want


def test_onchain_helpers(monkeypatch):
    w = lambda *xs: "0x" + "".join(format(x, "064x") for x in xs)
    calls = {}
    def fake(method, params, rpcs=None):
        data = params[0]["data"]
        calls[data[:10]] = params[0]["to"]
        if data.startswith("0x" + pf._sel("getPublicDrop(address)")):
            return w(10**15, 100, 200, 2, 1000, 1)
        return w(2, 43, 1000)
    monkeypatch.setattr(pf, "rpc", fake)
    nft = "0x" + "e6" * 20
    assert pf.onchain_public_drop(nft) == (10**15, 100, 200, 2)
    assert pf.onchain_public_price(nft) == 10**15
    assert pf.minted_count(nft, "0x" + "a8" * 20) == 2
    assert calls["0x" + pf._sel("getMintStats(address)")] == nft            # asked the NFT, not SeaDrop
    monkeypatch.setattr(pf, "rpc", lambda *a, **k: "0x")
    with pytest.raises(pf.PreflightError):
        pf.minted_count(nft, "0x" + "a8" * 20)
    assert pf.stage_wallet_limit({"max_per_wallet": "2"}) == 2
    assert pf.stage_wallet_limit({"max_per_wallet": None}) is None and pf.stage_wallet_limit({}) is None


def test_unit_bounds_and_speed_text():
    P = 10 ** 15
    assert Service.unit_bounds(P, "exact", 0) == (P, P)
    assert Service.unit_bounds(P, "lower_ok", 0) == (0, P)
    assert Service.unit_bounds(P, "cap", 10) == (P, P * 11 // 10)
    assert Service.unit_bounds(P, "any_up", 50) == (0, P * 3 // 2)
    assert Service.unit_bounds(0, "cap", 50) == (0, 0)            # free stays free
    from service.runner import speed_text
    st, msg = classify({"minted": True, "tx": "0xab", "token_ids": [1, 2], "wanted": 2,
                        "speed": {"api_win": 0.312, "sent": 0.351, "receipt": 0.52, "block": 9, "tx_index": 1,
                                  "block_delta": 0, "rank": 3, "unit_price": P}})
    assert st == "minted" and "+0.312" in msg and "+0.351" in msg and "+0.520" in msg and "#3" in msg and "0.001000" in msg
    assert speed_text({}) == ""
    st, msg = classify({"minted": False, "reason": "price_changed", "error": ""})
    assert st == "failed" and "قیمت" in msg
