"""Public fast path (mint_engine.FAST_PUBLIC): local mintPublic, simulated at T0 before T0, no OpenSea call at T0.
Real engine via the service wrapper, anvil fork of the real AERENT contract. Nothing reaches mainnet."""
import json, os, subprocess, sys, time

import pytest

from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea

pytestmark = pytest.mark.fork
P = 10 ** 15


@pytest.fixture(scope="module")
def env():
    fu.start_fork(); fo = FakeOpenSea()
    yield fo
    fo.close(); fu.stop_fork()


def run(fo, pk, slug, price, cap, start, end=None, extra=()):
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "9", "--slug", slug,
           "--nft", fu.NFT, "--label", "S", "--start", str(int(start)), "--end", str(int(end or start + 3600)),
           "--kind", "public", "--price-wei", str(price), "--max-value-wei", str(cap),
           "--api-base", fo.url, "--rpc", fu.F, "--no-api-check", *extra]
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


def chain_now():
    return int(fu.r("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)


def opensea_mint(fo, slug, price):
    fo.mint[slug] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": str(price), "chain": "robinhood"})


def mint_calls(fo, slug):
    return [c for c in fo.calls if c[0] == "POST" and f"/drops/{slug}/mint" in c[1]]


@pytest.mark.parametrize("price", [0, P])
def test_fast_path_mints_without_opensea(env, price):
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    t0 = int(time.time()) + 8
    fu.set_public_drop(price, t0, t0 + 3600)
    a, pk = fu.funded_wallet(10 ** 16)
    slug = f"fast{price}"; opensea_mint(fo, slug, price)
    b0 = fu.eth(a.address)
    out, res = run(fo, pk, slug, price, price, t0)
    assert res and res["minted"] and res["holds"] == 1, out[-2500:]
    assert "FAST PATH ready" in out and "FAST PATH: no OpenSea call" in out, out[-2500:]
    assert mint_calls(fo, slug) == []                               # OpenSea mint endpoint never called
    spent = b0 - fu.eth(a.address)
    assert price <= spent < price + 10 ** 14                          # exactly the price + gas
    assert res["speed"]["api_win"] < 0.05                             # "signature" ready at T0


def test_fallback_when_onchain_start_differs(env):
    """Public drop already open (start != task T0): fast path must stay off, normal OpenSea path mints."""
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600)
    a, pk = fu.funded_wallet()
    opensea_mint(fo, "fb1", 0)
    out, res = run(fo, pk, "fb1", 0, 0, time.time() + 4)
    assert res["holds"] == 1 and "fast path off (on-chain public start" in out, out[-2500:]
    assert len(mint_calls(fo, "fb1")) >= 1


def test_fallback_when_price_outside_policy(env):
    """On-chain price raised above what the user confirmed: fast path off; OpenSea path refuses -> nothing sent."""
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    t0 = int(time.time()) + 8
    fu.set_public_drop(2 * P, t0, t0 + 3600)
    a, pk = fu.funded_wallet(10 ** 16)
    opensea_mint(fo, "fb2", P)
    out, res = run(fo, pk, "fb2", P, P, t0)
    assert "fast path off (on-chain public price" in out, out[-2500:]
    assert res["holds"] == 0 and fu.nonce(a.address) == 0, out[-2500:]


def test_fallback_when_simulation_reverts(env):
    """Wallet already at the on-chain public per-wallet cap: the T0 simulation reverts -> fast path off, 0 gas."""
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    t0 = int(time.time()) + 8
    fu.set_public_drop(0, t0, t0 + 3600, max_per_wallet=0)
    a, pk = fu.funded_wallet()
    fo.mint["fb3"] = lambda m: (422, {"errors": ["This wallet cannot mint any more from this drop"]})
    out, res = run(fo, pk, "fb3", 0, 0, t0)
    assert "fast path off (simulation at T0 failed" in out, out[-2500:]
    assert res["holds"] == 0 and fu.nonce(a.address) == 0, out[-2500:]


def test_fast_path_cancelled_when_price_changes_before_t0(env):
    """Creator changes the price after the T-3 s preparation: the T-0.6 s re-check cancels the fast path."""
    import threading
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    t0 = int(time.time()) + 8
    fu.set_public_drop(P, t0, t0 + 3600)
    a, pk = fu.funded_wallet(10 ** 16)
    opensea_mint(fo, "fb4", P)

    def change():                                   # between T-3 s (prepare) and T-0.6 s (re-check)
        time.sleep(max(0, t0 - 1.6 - time.time())); fu.set_public_drop(2 * P, t0, t0 + 3600)
    th = threading.Thread(target=change); th.start()
    out, res = run(fo, pk, "fb4", P, P, t0)
    th.join()
    assert "FAST PATH ready" in out and "fast path cancelled: on-chain drop changed" in out, out[-2500:]
    assert res["holds"] == 0 and fu.nonce(a.address) == 0, out[-2500:]            # normal path also refuses


def test_soldout_revert_is_decoded(env):
    """Like live task #10: supply is free at T-45 s but the drop sells out right after T0 (rivals were faster).
    The tx reverts on-chain -> the engine must report 'sold_out' (decoded MintQuantityExceedsMaxSupply)."""
    from eth_utils import keccak
    from eth_abi import encode
    fo = env
    fu.r("anvil_setTime", [int(time.time())]); fu.r("evm_mine", [])
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=1000)
    a, pk = fu.funded_wallet()
    set_max = lambda n: fu.r("eth_sendTransaction", [{"from": fu.OWNER, "to": fu.NFT, "gas": hex(200000),
                             "data": "0x" + keccak(text="setMaxSupply(uint256)")[:4].hex() + encode(["uint256"], [n]).hex()}])
    t0 = time.time() + 4
    sent = {}

    def opensea(m):                                   # OpenSea answers; meanwhile the drop sells out (owner caps supply)
        fu.r("anvil_impersonateAccount", [fu.OWNER])
        total = int(fu.r("eth_call", [{"to": fu.NFT, "data": "0x18160ddd"}, "latest"]), 16)
        sent["h"] = set_max(total); fu.r("evm_mine", [])
        return 200, {"to": fu.SD, "data": fu.public_calldata(m), "value": "0", "chain": "robinhood"}
    fo.mint["so"] = opensea
    try:
        out, res = run(fo, pk, "so", 0, 0, t0)
        assert res["minted"] is False and res["reason"] == "sold_out", out[-2500:]
        assert "MintQuantityExceedsMaxSupply" in out, out[-2500:]
    finally:
        fu.r("anvil_impersonateAccount", [fu.OWNER]); set_max(1000); fu.r("evm_mine", [])
