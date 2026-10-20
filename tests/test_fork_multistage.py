"""Multi-quantity + multi-stage on the anvil fork of the REAL AERENT contract, via the real wrapper subprocess.
Everything on 127.0.0.1 (fork + fake OpenSea). Nothing reaches mainnet."""
import json, os, subprocess, sys, threading, time
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


def run(fo, pk, slug, kind, price, qty, start, label="S", nonce_file=None, end=None, cap=None):
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "31", "--slug", slug,
           "--nft", fu.NFT, "--label", label, "--start", str(int(start)), "--end", str(int(end or start + 3600)),
           "--kind", kind, "--price-wei", str(price), "--max-value-wei", str(price * qty if cap is None else cap),
           "--quantity", str(qty), "--api-base", fo.url, "--rpc", fu.F, "--no-api-check"]
    if nonce_file:
        cmd += ["--nonce-file", nonce_file]
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


def mints(addr):
    return int(fu.r("eth_call", [{"to": fu.NFT, "data": "0x" + __import__("eth_utils").keccak(text="getMintStats(address)")[:4].hex()
                                  + addr[2:].lower().rjust(64, "0")}, "latest"])[2:66], 16)


def test_public_paid_quantity_3_exact_value(env):
    _, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=5)
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["q3"] = lambda m, q: (200, {"to": fu.SD, "data": fu.public_calldata(m, q), "value": str(P * q), "chain": "robinhood"})
    n0 = len(fo.mint_bodies); b0 = fu.eth(a.address)
    out, res = run(fo, pk, "q3", "public", P, 3, time.time() + 4)
    assert res["minted"] and len(res["token_ids"]) == 3 and res["quantity"] == 3, out[-1500:]
    assert fu.holds(a.address) == 3 and fu.nonce(a.address) == 1                 # ONE tx for 3 tokens
    assert 3 * P <= b0 - fu.eth(a.address) < 3 * P + 10 ** 14
    assert all(b["quantity"] == 3 for b in fo.mint_bodies[n0:])                  # OpenSea was asked for 3


def test_cumulative_limit_clamps_quantity(env):
    """limit 3, wallet already minted 2 (earlier stage) -> user wanted 3, engine asks/sends 1, value = 1 x price."""
    _, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=3)
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["cl"] = lambda m, q: (200, {"to": fu.SD, "data": fu.public_calldata(m, q), "value": str(P * q), "chain": "robinhood"})
    out, res = run(fo, pk, "cl", "public", P, 2, time.time() + 4)
    assert res["minted"] and len(res["token_ids"]) == 2, out[-1500:]
    n0 = len(fo.mint_bodies); b0 = fu.eth(a.address)
    out, res = run(fo, pk, "cl", "public", P, 3, time.time() + 4)
    assert res["minted"] and res["token_ids"] and len(res["token_ids"]) == 1 and res["wanted"] == 3 and res["quantity"] == 1, out[-1500:]
    assert "wallet_limit" in out and fo.mint_bodies[n0]["quantity"] == 1
    assert P <= b0 - fu.eth(a.address) < P + 10 ** 14 and mints(a.address) == 3
    nn = fu.nonce(a.address)
    out, res = run(fo, pk, "cl", "public", P, 1, time.time() + 4)               # now full: zero tx
    assert res["minted"] is False and res["reason"] == "wallet_limit" and fu.nonce(a.address) == nn


def test_signed_free_quantity_5(env):
    ts, fo = env
    a, pk = fu.funded_wallet()
    fo.mint["sq"] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x5A + q, q=q, max_per_wallet=5),
                                         "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "sq", "signed", 0, 5, time.time() + 4)
    assert res["minted"] and len(res["token_ids"]) == 5 and fu.holds(a.address) == 5 and fu.nonce(a.address) == 1, out[-1500:]


def test_signed_limit_in_signature_saves_gas(env):
    """The signature's maxTotalMintableByWallet (2) < minted(0)+qty(3): engine must refuse locally, 0 tx."""
    ts, fo = env
    a, pk = fu.funded_wallet()
    fo.mint["sl"] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x51, q=q, max_per_wallet=2),
                                         "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "sl", "signed", 0, 3, time.time() + 4)
    assert res["minted"] is False and res["reason"] == "wallet_limit" and fu.nonce(a.address) == 0, out[-1500:]


def test_signature_for_other_stage_refused(env):
    """OpenSea serves 'the first eligible stage': a signature whose window is not our stage is never sent."""
    ts, fo = env
    a, pk = fu.funded_wallet()
    now = int(time.time())
    fo.mint["os"] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x52, start=now - 60, end=now + 30, q=q),
                                         "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "os", "signed", 0, 1, now + 120, end=now + 3600)    # our stage starts at +120 s
    assert res["minted"] is False and fu.nonce(a.address) == 0 and "does not overlap" in out, out[-1500:]


def test_two_stages_same_wallet_back_to_back(env, tmp_path):
    """Allowlist (signed, 2 free) and Public (paid, 3) for the SAME wallet, overlapping in time, run concurrently
    as two processes sharing the per-wallet nonce file. Both must land: 5 tokens, 2 txs, no nonce collision."""
    ts, fo = env
    now = int(time.time()); fu.set_public_drop(P, now - 60, now + 3600, max_per_wallet=10)
    a, pk = fu.funded_wallet(10 ** 16)
    fo.mint["al"] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x77, q=q, max_per_wallet=10),
                                         "value": "0", "chain": "robinhood"})
    fo.mint["pb"] = lambda m, q: (200, {"to": fu.SD, "data": fu.public_calldata(m, q), "value": str(P * q), "chain": "robinhood"})
    nf = str(tmp_path / "wallet-1.nonce")
    out = {}
    t0 = time.time() + 5
    th = [threading.Thread(target=lambda: out.__setitem__("al", run(fo, pk, "al", "signed", 0, 2, t0, "Allowlist", nf))),
          threading.Thread(target=lambda: out.__setitem__("pb", run(fo, pk, "pb", "public", P, 3, t0, "Public", nf)))]
    [t.start() for t in th]; [t.join(160) for t in th]
    ra, rb = out["al"][1], out["pb"][1]
    assert ra["minted"] and rb["minted"], (out["al"][0][-1500:], out["pb"][0][-1500:])
    assert len(ra["token_ids"]) == 2 and len(rb["token_ids"]) == 3
    assert fu.holds(a.address) == 5 and mints(a.address) == 5
    assert fu.nonce(a.address) == 2                                                # exactly 2 txs, no wasted gas
