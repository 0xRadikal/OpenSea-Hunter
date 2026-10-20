"""Engine wrapper as a real subprocess against the anvil fork + fake OpenSea. Nothing reaches mainnet."""
import json, os, subprocess, sys, time
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea

pytestmark = pytest.mark.fork
PY = sys.executable

@pytest.fixture(scope="module")
def env():
    fu.start_fork(); ts = fu.register_test_signer(); fo = FakeOpenSea()
    yield ts, fo
    fo.close(); fu.stop_fork()

def run_wrapper(fo, pk, slug, start, extra=(), secret=None):
    cmd = [PY, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "1", "--slug", slug, "--nft", fu.NFT,
           "--label", "OG List", "--start", str(int(start)), "--end", str(int(start) + 3600),
           "--kind", "signed", "--price-wei", "0", "--max-value-wei", "0",
           "--api-base", fo.url, "--rpc", fu.F, "--no-api-check", *extra]
    p = subprocess.run(cmd, input=(secret if secret is not None else json.dumps({"pk": pk, "api_key": "k" * 32})) + "\n",
                       capture_output=True, text=True, timeout=150)
    last = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p, (json.loads(last[-1][11:]) if last else None)

def test_mint_and_secret_hygiene(env):
    ts, fo = env
    a, pk = fu.funded_wallet()
    cd = fu.signed_calldata(ts, a.address, 0xA1)
    n = {"c": 0}
    def mint(minter):
        n["c"] += 1
        assert minter.lower() == a.address.lower()
        return (409, {"errors": ["Drop is not currently active for minting"]}) if n["c"] == 1 else (200, {"to": fu.SD, "data": cd, "value": "0", "chain": "robinhood"})
    fo.mint["s1"] = mint; fo.drops["s1"] = {"stages": [{"label": "OG List", "uuid": "u"}]}; fo.active_after["s1"] = 0
    p, res = run_wrapper(fo, pk, "s1", time.time() + 4)
    out = p.stdout + p.stderr
    assert res and res["minted"] is True and res["holds"] == 1 and res["tx"] and len(res["token_ids"]) == 1, out[-2000:]
    assert "MINTED" in out
    assert pk[2:] not in out and pk not in out and "k" * 32 not in out            # secrets masked in log
    assert fu.holds(a.address) == 1

def test_public_calldata_refused_zero_tx(env):
    ts, fo = env
    a, pk = fu.funded_wallet()
    from eth_abi import encode
    from eth_utils import keccak
    pub = "0x" + keccak(text="mintPublic(address,address,address,uint256)")[:4].hex() + encode(["address","address","address","uint256"], [fu.NFT, fu.FEE, "0x"+"0"*40, 1]).hex()
    fo.mint["s2"] = lambda m: (200, {"to": fu.SD, "data": pub, "value": str(10**17), "chain": "robinhood"})
    p, res = run_wrapper(fo, pk, "s2", time.time() + 4)
    assert res and res["minted"] is False and res["holds"] == 0 and res["tx"] is None
    assert int(fu.r("eth_getTransactionCount", [a.address, "latest"]), 16) == 0
    assert "REFUSED" in p.stdout

def test_bad_secret_input(env):
    _, fo = env
    p, res = run_wrapper(fo, "", "s1", time.time() + 4, secret="garbage")
    assert res == {"minted": False, "tx": None, "token_ids": [], "reason": "bad_input", "error": "bad secret input", "holds": -1}

def test_dry_run_never_broadcasts(env):
    ts, fo = env
    a, pk = fu.funded_wallet()
    cd = fu.signed_calldata(ts, a.address, 0xA3)
    fo.mint["s3"] = lambda m: (200, {"to": fu.SD, "data": cd, "value": "0", "chain": "robinhood"})
    p, res = run_wrapper(fo, pk, "s3", time.time() + 4, extra=["--dry-run"])
    assert "DRY-RUN: eth_call -> OK" in p.stdout, p.stdout[-1500:]
    assert int(fu.r("eth_getTransactionCount", [a.address, "latest"]), 16) == 0 and res["holds"] == 0 and res["minted"] is False
