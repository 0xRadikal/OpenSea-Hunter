"""LIVE 2026-10-21 (nazism-doc): per-wallet limits are PER STAGE. Wallet minted 4 in signed stage "x" (limit 4);
public stage limit 1 on-chain (getPublicDrop maxPerWallet=1, numMinted=4) — yet OpenSea served the public stage as
mintSigned with maxTotalMintableByWallet=5 (= 4 + 1) and the wallet minted its 5th token (tx 0xcb43d3bd...).
The old engine refused up front ("wallet_limit", 4/1). Reproduced here on the fork of the REAL AERENT contract."""
import json, os, subprocess, sys, time
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea

pytestmark = pytest.mark.fork


@pytest.fixture(scope="module")
def env():
    fu.start_fork(); ts = fu.register_test_signer(); fo = FakeOpenSea()
    yield ts, fo
    fo.close(); fu.stop_fork()


def run(fo, pk, slug, kind, qty, start, uuid=None):
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "61", "--slug", slug,
           "--nft", fu.NFT, "--label", "S", "--start", str(int(start)), "--end", str(int(start) + 3600),
           "--kind", kind, "--price-wei", "0", "--max-value-wei", "0", "--quantity", str(qty),
           "--api-base", fo.url, "--rpc", fu.F, "--no-api-check"] + (["--stage-uuid", uuid] if uuid else [])
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


def drop(fo, slug):
    fo.drops[slug] = {"collection_slug": slug, "chain": "robinhood", "contract_address": fu.NFT,
                      "stages": [{"uuid": "u-x", "label": "x", "stage_type": "signed_presale", "price": "0", "max_per_wallet": "4"},
                                 {"uuid": "u-pub", "label": "Public stage", "stage_type": "public_sale", "price": "0", "max_per_wallet": "1"}]}


def test_public_stage_served_signed_with_per_stage_cap(env):
    ts, fo = env
    a, pk = fu.funded_wallet()
    slug = "naz"; drop(fo, slug); fo.eligible.add(a.address.lower())
    # stage x: mint 4 (signed, cap 4)
    fo.caps[(slug, "u-x")] = 4
    fo.mint[slug] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x4A, q=q, max_per_wallet=4), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, slug, "signed", 4, time.time() + 4, "u-x")
    assert res["minted"] and len(res["token_ids"]) == 4, out[-1500:]
    # public stage: on-chain public limit 1 already "used" by numMinted=4, OpenSea serves mintSigned cap 5
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=1)
    fo.caps[(slug, "u-pub")] = 5
    fo.mint[slug] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x5B, q=q, max_per_wallet=5), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, slug, "public", 1, time.time() + 4, "u-pub")
    assert res["minted"] and len(res["token_ids"]) == 1, out[-2000:]
    assert "cap 5 (OpenSea eligibility)" in out
    assert fu.holds(a.address) == 5
    # now the public stage cap (5) is reached -> engine stops with 0 tx
    n0 = fu.nonce(a.address)
    out, res = run(fo, pk, slug, "public", 1, time.time() + 4, "u-pub")
    assert res["minted"] is False and res["reason"] == "wallet_limit" and fu.nonce(a.address) == n0, out[-1500:]


def test_limit_raised_mid_stage_is_followed(env):
    """Creator raises stage x from 3 to 4 while it runs (live): OpenSea cap 4 -> 1 more mintable after 3."""
    ts, fo = env
    a, pk = fu.funded_wallet()
    slug = "raise"; drop(fo, slug); fo.eligible.add(a.address.lower())
    fo.caps[(slug, "u-x")] = 3
    fo.mint[slug] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x31 + q, q=q, max_per_wallet=3), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, slug, "signed", 5, time.time() + 4, "u-x")          # wants 5, cap 3 -> 3
    assert res["minted"] and len(res["token_ids"]) == 3 and res["quantity"] == 3, out[-1500:]
    fo.caps[(slug, "u-x")] = 4
    fo.mint[slug] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x41 + q, q=q, max_per_wallet=4), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, slug, "signed", 3, time.time() + 4, "u-x")          # wants 3, cap 4 - minted 3 -> 1
    assert res["minted"] and len(res["token_ids"]) == 1 and res["quantity"] == 1, out[-1500:]


def test_opensea_says_cannot_mint_any_more_stops(env):
    """OpenSea 422 'cannot mint any more' (live text): engine stops after 2 answers, 0 tx, clear reason."""
    _, fo = env
    a, pk = fu.funded_wallet()
    fo.mint["nomore"] = lambda m, q: (422, {"errors": ["This wallet cannot mint any more from this drop: it has reached its limit for the active stage, or the drop is minted out"]})
    t = time.time()
    out, res = run(fo, pk, "nomore", "public", 1, time.time() + 4)
    assert res["minted"] is False and res["reason"] == "wallet_limit" and fu.nonce(a.address) == 0, out[-1500:]
    assert time.time() - t < 40 and "reached its limit for the active stage" in out


def test_cap_unknown_does_not_block(env):
    """No eligibility (no uuid): engine must not pre-refuse; OpenSea/contract decide."""
    ts, fo = env
    a, pk = fu.funded_wallet()
    fo.mint["nocap"] = lambda m, q: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x77, q=q, max_per_wallet=2), "value": "0", "chain": "robinhood"})
    out, res = run(fo, pk, "nocap", "signed", 2, time.time() + 4)
    assert res["minted"] and len(res["token_ids"]) == 2 and "per-wallet cap unknown" in out, out[-1500:]
