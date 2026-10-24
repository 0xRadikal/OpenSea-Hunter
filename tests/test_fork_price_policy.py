"""Creator changes the price at/after stage start — engine reaction per user policy, on the fork of the REAL
AERENT contract via the real wrapper. Also checks the speed report fields. Nothing reaches mainnet."""
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


def run(fo, pk, slug, kind, price, qty, start, lo, hi, pre=""):
    env = dict(os.environ)
    if pre:
        import tempfile, textwrap
        d = tempfile.mkdtemp(); open(os.path.join(d, "sitecustomize.py"), "w").write(textwrap.dedent(pre))
        env["PYTHONPATH"] = d + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, os.path.join(fu.ROOT, "service", "engine_wrapper.py"), "--task-id", "41", "--slug", slug,
           "--nft", fu.NFT, "--label", "S", "--start", str(int(start)), "--end", str(int(start) + 3600),
           "--kind", kind, "--price-wei", str(price), "--max-value-wei", str(hi * qty), "--quantity", str(qty),
           "--min-unit-wei", str(lo), "--max-unit-wei", str(hi), "--api-base", fo.url, "--rpc", fu.F, "--no-api-check"]
    p = subprocess.run(cmd, input=json.dumps({"pk": pk, "api_key": "k" * 32}) + "\n", capture_output=True, text=True, timeout=150, env=env)
    res = [l for l in p.stdout.splitlines() if l.startswith("OSH_RESULT ")]
    return p.stdout + p.stderr, (json.loads(res[-1][11:]) if res else None)


def public(fo, slug, served_price):
    fo.mint[slug] = lambda m, q: (200, {"to": fu.SD, "data": fu.public_calldata(m, q), "value": str(served_price * q), "chain": "robinhood"})


@pytest.mark.parametrize("policy,lo,hi,new_price,expect_mint", [
    ("exact", P, P, 2 * P, False),
    ("exact", P, P, P // 2, False),
    ("lower_ok", 0, P, P // 2, True),
    ("lower_ok", 0, P, 2 * P, False),
    ("cap+50%", P, P * 3 // 2, P * 3 // 2, True),
    ("cap+50%", P, P * 3 // 2, 2 * P, False),
    ("any_up+100%", 0, 2 * P, 2 * P, True),
    ("any_up+100%", 0, 2 * P, P // 4, True),
])
def test_public_price_changed_before_t0(env, policy, lo, hi, new_price, expect_mint):
    """Confirmed P; creator changed the price on-chain and OpenSea now serves the new value."""
    _, fo = env
    now = int(time.time()); fu.set_public_drop(new_price, now - 60, now + 3600, max_per_wallet=10)
    a, pk = fu.funded_wallet(10 ** 16)
    slug = "pp" + str(abs(hash((policy, new_price))) % 10 ** 6)
    public(fo, slug, new_price)
    b0 = fu.eth(a.address)
    out, res = run(fo, pk, slug, "public", P, 2, time.time() + 4, lo, hi)
    if expect_mint:
        assert res["minted"] and len(res["token_ids"]) == 2, out[-1500:]
        spent = b0 - fu.eth(a.address)
        assert 2 * new_price <= spent < 2 * new_price + 10 ** 14 and spent <= 2 * hi + 10 ** 14
        assert res["speed"]["unit_price"] == new_price
    else:
        assert res["minted"] is False and res["reason"] == "price_changed" and fu.nonce(a.address) == 0, out[-1500:]


def test_public_price_raised_after_opensea_answered(env):
    """OpenSea served value at the OLD price, creator raised the on-chain price before we signed. mintPublic charges
    the CURRENT price: within policy -> engine pays the new price; outside -> nothing sent."""
    _, fo = env
    for hi, expect in ((P * 3 // 2, True), (P, False)):
        now = int(time.time()); fu.set_public_drop(P * 3 // 2, now - 60, now + 3600, max_per_wallet=10)
        a, pk = fu.funded_wallet(10 ** 16)
        slug = f"late{hi}"
        public(fo, slug, P)                                          # stale OpenSea answer
        out, res = run(fo, pk, slug, "public", P, 1, time.time() + 4, P, hi)
        if expect:
            assert res["minted"] and res["speed"]["unit_price"] == P * 3 // 2 and "within policy" in out, out[-1500:]
        else:
            assert res["minted"] is False and res["reason"] == "price_changed" and fu.nonce(a.address) == 0, out[-1500:]


def test_signed_price_changed(env):
    ts, fo = env
    for lo, hi, signed, expect in ((P, P, 2 * P, False), (P, 2 * P, 2 * P, True), (0, P, P // 2, True)):
        a, pk = fu.funded_wallet(10 ** 16)
        slug = f"sp{lo}{hi}{signed}"
        fo.mint[slug] = lambda m, q, a=a, signed=signed: (200, {"to": fu.SD, "data": fu.signed_calldata(ts, a.address, 0x99 + signed % 97, price=signed, q=q, max_per_wallet=10),
                                                                 "value": str(signed * q), "chain": "robinhood"})
        b0 = fu.eth(a.address)
        out, res = run(fo, pk, slug, "signed", P, 1, time.time() + 4, lo, hi)
        if expect:
            assert res["minted"], out[-1500:]
            assert signed <= b0 - fu.eth(a.address) < signed + 10 ** 14
        else:
            assert res["minted"] is False and res["reason"] == "price_changed" and fu.nonce(a.address) == 0, out[-1500:]


def test_speed_report_and_rank(env):
    """Two competitor mints land in the T0 window before ours -> our rank must be 3; timings are monotonic."""
    _, fo = env
    t0 = time.time() + 6
    now = int(time.time()); fu.set_public_drop(0, now - 60, now + 3600, max_per_wallet=10)
    a, pk = fu.funded_wallet()
    slow = "import time as _t\nimport requests as _r\n_p=_r.Session.post\n" \
           "def post(self,url,json=None,**k):\n" \
           "    if isinstance(json,dict) and json.get('method')=='eth_sendRawTransaction': _t.sleep(1.5)\n" \
           "    return _p(self,url,json=json,**k)\n_r.Session.post=post\n"
    public(fo, "spd", 0)
    import threading
    def rivals():
        while time.time() < t0 + 0.3:
            time.sleep(0.01)
        for _ in range(2):
            b, _pk = fu.funded_wallet()
            fu.r("anvil_impersonateAccount", [b.address])
            fu.r("eth_sendTransaction", [{"from": b.address, "to": fu.SD, "data": fu.public_calldata(b.address, 1), "gas": hex(600000)}])
    th = threading.Thread(target=rivals); th.start()
    out, res = run(fo, pk, "spd", "public", 0, 1, t0, 0, 0, pre=slow)
    th.join()
    sp = res["speed"]
    assert res["minted"], out[-1500:]
    assert 0 <= sp["api_win"] <= sp["sent"] <= sp["receipt"] < 30, sp
    assert sp["rank"] == 3 and sp["block"] > 0 and "block_delta" in sp, sp
