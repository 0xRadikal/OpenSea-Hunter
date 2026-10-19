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
