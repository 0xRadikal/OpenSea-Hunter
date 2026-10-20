"""Regressions for bugs seen LIVE on 2026-10-20 (an-asshole). Each test reproduces the exact live condition on the fork."""
import json, os, subprocess, sys, time, textwrap
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea

pytestmark = pytest.mark.fork


@pytest.fixture(scope="module")
def env():
    fu.start_fork(); fu.register_test_signer(); fo = FakeOpenSea()
    yield fo
    fo.close(); fu.stop_fork()


def run(fo, pk, slug, start, pre=""):
    """Run the real wrapper; `pre` = python injected via sitecustomize to simulate a live RPC condition."""
    env = dict(os.environ)
    d = None
    if pre:
        import tempfile
        d = tempfile.mkdtemp()
        open(os.path.join(d, "sitecustomize.py"), "w").write(textwrap.dedent(pre))
        env["PYTHONPATH"] = d + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "77", "--slug", slug,
           "--nft", fu.NFT, "--label", "Public stage", "--start", str(int(start)), "--end", str(int(start) + 3600),
           "--kind", "public", "--price-wei", "0", "--max-value-wei", "0",
           "--api-base", fo.url, "--rpc", fu.F, "--no-api-check"]
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150, env=env)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


# Live bug #1: tx mined (status 1, Transfer to wallet) but balanceOf read right after the receipt returned the
# PREVIOUS value (lagging RPC) -> OSH_RESULT holds=0 target=1 -> reported "failed".
LAGGING_BALANCE = """
import builtins, json, time
_orig = builtins.__import__
def _imp(name, *a, **k):
    m = _orig(name, *a, **k)
    if name == "aerent_engine_patch_target":
        pass
    return m
import requests
_post = requests.Session.post
STATE = {"calls": 0}
def post(self, url, json=None, **kw):
    r = _post(self, url, json=json, **kw)
    if isinstance(json, dict) and json.get("method") == "eth_call" and str(json["params"][0].get("data", "")).startswith("0x70a08231"):
        STATE["calls"] += 1
        if STATE["calls"] >= 2:                       # every balance read after the pre-flight one lags (returns 0)
            class R:
                status_code = 200
                def json(_s): return {"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 64}
            return R()
    return r
requests.Session.post = post
"""


def test_live_bug1_success_reported_as_failed(env):
    fo = env
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=5)
    a, pk = fu.funded_wallet()
    fo.mint["lag"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "lag", time.time() + 4, LAGGING_BALANCE)
    assert fu.holds(a.address) == 1, out[-1200:]                 # it really minted on-chain
    assert res["minted"] is True and res["tx"] and res["token_ids"], (res, out[-1200:])


# Live bug #2: an RPC answered without "result" during pre-flight -> raw "KeyError: 'result'" shown to the user.
NO_RESULT_ONCE = """
import requests
_post = requests.Session.post
STATE = {"n": 0}
def post(self, url, json=None, **kw):
    if isinstance(json, dict) and json.get("method") in ("eth_chainId", "eth_getCode") and STATE["n"] < 2:
        STATE["n"] += 1
        class R:
            status_code = 200
            def json(_s): return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "temporary internal error"}}
        return R()
    return _post(self, url, json=json, **kw)
requests.Session.post = post
"""


def test_live_bug2_transient_rpc_error_in_preflight(env):
    fo = env
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=5)
    a, pk = fu.funded_wallet()
    fo.mint["tr"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "tr", time.time() + 4, NO_RESULT_ONCE)
    assert "KeyError" not in out, out[-1200:]
    assert res["minted"] is True and fu.holds(a.address) == 1, out[-1200:]


# Live bug #3: wallet already at the on-chain per-wallet limit -> engine must stop with a clear reason, 0 tx.
def test_live_bug3_wallet_limit_reached(env):
    fo = env
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=1)
    a, pk = fu.funded_wallet()
    fo.mint["wl"] = lambda m: (200, {"to": fu.SD, "data": fu.public_calldata(m), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "wl", time.time() + 4)
    assert res["minted"] is True and fu.holds(a.address) == 1
    n0 = fu.nonce(a.address)
    out, res = run(fo, pk, "wl", time.time() + 4)
    assert res["minted"] is False and res["reason"] == "wallet_limit", (res, out[-1000:])
    assert fu.nonce(a.address) == n0                                # not a single extra tx / gas
