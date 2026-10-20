"""mint_engine.py via the real service wrapper subprocess, against the anvil fork of the real AERENT contract.
Paid + public paths. Nothing reaches mainnet (all RPC = 127.0.0.1)."""
import json, os, subprocess, sys, time
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea

pytestmark = pytest.mark.fork
P = 10 ** 15


@pytest.fixture(scope="module")
def env():
    fu.start_fork(); ts = fu.register_test_signer(); fo = FakeOpenSea()
    yield ts, fo
    fo.close(); fu.stop_fork()


def run(fo, pk, slug, kind, price, cap, start, extra=()):
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "9", "--slug", slug,
           "--nft", fu.NFT, "--label", "S", "--start", str(int(start)), "--end", str(int(start) + 3600),
           "--kind", kind, "--price-wei", str(price), "--max-value-wei", str(cap),
           "--api-base", fo.url, "--rpc", fu.F, "--no-api-check", *extra]
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


def test_free_public(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600)
    a, pk = fu.funded_wallet()
    fo.mint["fp"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "fp", "public", 0, 0, time.time() + 4)
    assert res["holds"] == 1, out[-1500:]
    assert "MINTED" in out


def test_paid_public_exact_value(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600)
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["pp"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(P), "chain": "robinhood"})
    b0 = fu.eth(a.address)
    out, res = run(fo, pk, "pp", "public", P, P, time.time() + 4)
    spent = b0 - fu.eth(a.address)
    assert res["holds"] == 1, out[-1500:]
    assert P <= spent < P + 10 ** 14            # exactly the price + gas (gas measured << 0.0001 ETH)


def test_paid_public_price_raised_onchain_refused(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(2 * P, now - 60, now + 3600)     # creator raised price after confirm
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["pr"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(P), "chain": "robinhood"})
    out, res = run(fo, pk, "pr", "public", P, P, time.time() + 4)
    assert res["holds"] == 0 and fu.nonce(a.address) == 0, out[-1500:]
    assert "on-chain public price" in out


def test_opensea_asks_more_than_confirmed_refused(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600)
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["om"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(2 * P), "chain": "robinhood"})
    out, res = run(fo, pk, "om", "public", P, 3 * P, time.time() + 4)
    assert res["holds"] == 0 and fu.nonce(a.address) == 0 and "REFUSED" in out


def test_paid_signed(env):
    ts, fo = env
    a, pk = fu.funded_wallet(10 ** 16)
    # signer params on the fork allow price >= 0 (minMintPrice 0); sign a paid mint
    cd = fu.signed_calldata(ts, a.address, int(time.time() * 1000), price=P)
    fo.mint["ps"] = lambda m: (200, {"to": fu.SD, "data": cd, "value": str(P), "chain": "robinhood"})
    b0 = fu.eth(a.address)
    out, res = run(fo, pk, "ps", "signed", P, P, time.time() + 4)
    assert res["holds"] == 1, out[-1500:]
    assert P <= b0 - fu.eth(a.address) < P + 10 ** 14


def test_balance_below_price_refused(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600)
    a, pk = fu.funded_wallet(P // 2)
    fo.mint["lb"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(P), "chain": "robinhood"})
    out, res = run(fo, pk, "lb", "public", P, P, time.time() + 50)    # >45 s so balance check happens before T0
    assert res["holds"] == 0 and fu.nonce(a.address) == 0 and "does not cover mint price" in out, out[-800:]
