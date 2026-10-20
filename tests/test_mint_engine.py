"""Validator of mint_engine.py (generic policy engine). Uses REAL OpenSea calldata fixtures."""
import importlib.util, json, os
import pytest
from eth_abi import encode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = "0x0000a26b00c1f0df003000390027140000faa719"
ME = "0xa842e3aB069562Db379c35Dd52C77ef214324004"          # wallet the live fixture was built for


def eng(**kw):
    sp = importlib.util.spec_from_file_location("me_" + str(id(kw)), os.path.join(ROOT, "mint_engine.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def signed_cd(m, price=0, minter="0x" + "0" * 40, q=1, nft=None):
    return m.SEL_SIGNED + encode(["address", "address", "address", "uint256", "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "uint256", "bytes"],
                                 [nft or m.NFT, FEE, minter, q, (price, 1, 1, 2, 1, 1000, 1000, True), 7, b"\x11" * 65]).hex()


def public_cd(m, minter=ME, q=1, nft=None):
    return m.SEL_PUBLIC + encode(["address", "address", "address", "uint256"], [nft or m.NFT, FEE, minter, q]).hex() + "3d958fe2"


NFT_A = "0xe6e34769c306d1368c0d938c5936eeca47d2e753"

def test_real_public_fixture_free():
    m = eng(NFT=NFT_A, MINT_KIND="public", EXPECTED_PRICE=0, MAX_VALUE_WEI=0)
    real = json.load(open(os.path.join(ROOT, "fixture_public_anasshole.json")))
    assert m.validate(real, ME) == (True, "mintPublic", 0)
    assert m.validate(real, "0x" + "1" * 40)[0] is False                    # recipient is another wallet

def test_real_signed_fixtures_still_ok_in_signed_mode():
    m = eng(NFT="0x728924f13327f17b5be06b00fd26a6df2a2aacb6", MINT_KIND="signed", EXPECTED_PRICE=0, MAX_VALUE_WEI=0)
    real = json.load(open(os.path.join(ROOT, "fixture_testdrop_mint.json")))
    assert m.validate(real, "0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697")[0] is True
    m2 = eng(NFT="0x0b10721af018aadfbb3e31d0f4d2dea2d6bd994b")
    d = open(os.path.join(ROOT, "fixture_thousand_calldata.txt")).read()
    assert m2.validate({"to": m2.SD, "data": d, "value": "0"}, "0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697")[0] is True
    assert m2.validate({"to": m2.SD, "data": d + "00" * 80, "value": "0"}, "0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697")[0] is False

P = 10 ** 15
@pytest.mark.parametrize("kind,price,cap,value,cdkw,ok", [
    ("signed", P, P, str(P), dict(price=P), True),                 # paid signed, exact
    ("signed", P, P, str(P), dict(price=P - 1), False),            # signed price != value
    ("signed", P, P, str(P + 1), dict(price=P + 1), False),        # value != confirmed price
    ("signed", P, P - 1, str(P), dict(price=P), False),            # over spend cap (bad policy)
    ("signed", 0, 0, "0", dict(price=0), True),                    # free signed
    ("signed", 0, 0, "1", dict(price=0), False),                   # value > 0 on free
    ("signed", P, P, str(P), dict(price=P, q=2), False),           # quantity 2
    ("signed", P, P, str(P), dict(price=P, minter="0x" + "3" * 40), False),
    ("signed", P, P, str(P), dict(price=P, nft="0x" + "2" * 40), False),
    ("public", P, 2 * P, str(P), {}, True),                        # paid public, exact
    ("public", P, 2 * P, str(2 * P), {}, False),                   # OpenSea asks more than confirmed
    ("public", P, 2 * P, "0", {}, False),                          # OpenSea asks less than confirmed
    ("public", 0, 0, "0", dict(q=2), False),
    ("public", 0, 0, "0", dict(nft="0x" + "2" * 40), False),
])
def test_policy_matrix(kind, price, cap, value, cdkw, ok):
    m = eng(NFT=NFT_A, MINT_KIND=kind, EXPECTED_PRICE=price, MAX_VALUE_WEI=cap)
    data = signed_cd(m, **cdkw) if kind == "signed" else public_cd(m, **cdkw)
    r = m.validate({"to": m.SD, "data": data, "value": value}, ME)
    assert r[0] is ok, r
    if ok:
        assert r[2] == int(value)

def test_kind_crossover_refused():
    m = eng(NFT=NFT_A, MINT_KIND="signed", EXPECTED_PRICE=0, MAX_VALUE_WEI=0)
    assert m.validate({"to": m.SD, "data": public_cd(m), "value": "0"}, ME)[0] is False   # public calldata in signed task
    m = eng(NFT=NFT_A, MINT_KIND="public", EXPECTED_PRICE=0, MAX_VALUE_WEI=0)
    assert m.validate({"to": m.SD, "data": signed_cd(m), "value": "0"}, ME)[0] is False   # signed calldata in public task

def test_wrong_target_and_junk():
    m = eng(NFT=NFT_A, MINT_KIND="public", EXPECTED_PRICE=0, MAX_VALUE_WEI=0)
    assert m.validate({"to": "0x" + "1" * 40, "data": public_cd(m), "value": "0"}, ME)[0] is False
    assert m.validate({"to": m.SD, "data": public_cd(m) + "00" * 80, "value": "0"}, ME)[0] is False
    assert m.validate(None, ME)[0] is False and m.validate({"to": m.SD}, ME)[0] is False

def test_original_engine_untouched_still_refuses_public():
    sp = importlib.util.spec_from_file_location("orig", os.path.join(ROOT, "aerent_mint.py"))
    o = importlib.util.module_from_spec(sp); sp.loader.exec_module(o)
    o.NFT = NFT_A
    real = json.load(open(os.path.join(ROOT, "fixture_public_anasshole.json")))
    assert o.validate(real, ME)[0] is False            # this is exactly why the live 'Public stage' was refused
