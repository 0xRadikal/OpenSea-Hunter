import pytest
from service.opensea import parse_link, OpenSeaError, iso_ts

@pytest.mark.parametrize("s,exp", [
    ("aerent", ("slug", "aerent")),
    ("https://opensea.io/collection/aerent", ("slug", "aerent")),
    ("opensea.io/collection/AERENT/overview", ("slug", "aerent")),
    ("https://opensea.io/collection/bottest-464316076/drop?x=1", ("slug", "bottest-464316076")),
    ("https://www.opensea.io/fa/collection/aerent", ("slug", "aerent")),
    ("https://opensea.io/item/robinhood/0xa655bbe47a8dec02991452adc7bef76bbbeceee9/5",
     ("contract", ("robinhood", "0xa655bbe47a8dec02991452adc7bef76bbbeceee9"))),
])
def test_parse_ok(s, exp):
    assert parse_link(s) == exp

@pytest.mark.parametrize("s", ["", "https://evil.com/collection/aerent", "https://opensea.io.evil.com/collection/x",
                               "https://opensea.io/", "https://opensea.io/collection/", "a b", "https://opensea.io/item/robinhood/0x12"])
def test_parse_bad(s):
    with pytest.raises(OpenSeaError): parse_link(s)

def test_iso():
    assert iso_ts("2026-10-03T16:30:00Z") == 1791045000


def test_stage_uuid_dash_formats_match():
    """Live 2026-10-24: /drops/{slug} gave '2f7439221e394db9a295d7aa3cc149e8' while /eligibility gave
    '2f743922-1e39-4db9-a295-d7aa3cc149e8' for the same stage -> must be the same key."""
    from service.opensea import norm_uuid
    assert norm_uuid("2f7439221e394db9a295d7aa3cc149e8") == norm_uuid("2f743922-1e39-4db9-a295-d7aa3cc149e8")
    assert norm_uuid("42A54064-9112-45d3-8ad4-04173d582e34") == norm_uuid("42a54064911245d38ad404173d582e34")
    assert norm_uuid(None) == ""


def test_eligibility_keys_normalized(monkeypatch):
    from service.opensea import OpenSea, norm_uuid
    o = OpenSea("http://x")
    monkeypatch.setattr(o, "_get", lambda *a, **k: {"stages": [
        {"stage_uuid": "2f743922-1e39-4db9-a295-d7aa3cc149e8", "is_eligible": True, "max_total_mintable_by_wallet": "1", "price": "0"}]})
    el = o.eligibility_full("s", "k", "j")
    assert el[norm_uuid("2f7439221e394db9a295d7aa3cc149e8")] == {"eligible": True, "cap": 1, "price": 0}
