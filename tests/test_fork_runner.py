import os, time
import pytest
from tests import forkutil as fu
from tests.fake_opensea import FakeOpenSea
from service.db import DB
from service.vault import Vault
from service.config import Config
from service.runner import Runner, pid_is_task

pytestmark = pytest.mark.fork

@pytest.fixture(scope="module")
def env():
    fu.start_fork(); ts = fu.register_test_signer(); fo = FakeOpenSea()
    yield ts, fo
    fo.close(); fu.stop_fork()

def mk(tmp_path, fo):
    cfg = Config(); cfg.data_dir = str(tmp_path); cfg.spawn_before_s = 30
    db = DB(cfg.db_path); v = Vault(os.urandom(32)); msgs = []
    rn = Runner(db, v, cfg, lambda uid, m: msgs.append((uid, m)),
                extra_args=["--api-base", fo.url, "--rpc", fu.F, "--no-api-check"])
    return db, v, rn, msgs

def add(db, v, uid, pk, slug, start, sub=True):
    db.ensure_user(uid)
    if sub: db.extend_sub(uid, 30)
    addr, blob = v.encrypt(uid, pk); wid = db.add_wallet(uid, addr, blob)
    db.set_apikey(uid, v.seal(uid, "apikey", "k" * 32), int(time.time()) + 7 * 86400, "test")
    return db.add_task(uid, wid, slug, fu.NFT, "OG List", int(start), int(start) + 3600)

def run_until(rn, db, tid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        rn.tick()
        if db.task(tid)["status"] not in ("scheduled", "running"): return db.task(tid)
        time.sleep(0.3)
    raise AssertionError("timeout " + str(dict(db.task(tid))))

def test_full_pipeline_two_users_parallel(tmp_path, env):
    ts, fo = env
    db, v, rn, msgs = mk(tmp_path, fo)
    a1, pk1 = fu.funded_wallet(); a2, pk2 = fu.funded_wallet()
    cds = {a1.address.lower(): fu.signed_calldata(ts, a1.address, 0xB1), a2.address.lower(): fu.signed_calldata(ts, a2.address, 0xB2)}
    fo.mint["p"] = lambda m: (200, {"to": fu.SD, "data": cds[m.lower()], "value": "0", "chain": "robinhood"})
    start = time.time() + 6
    t1 = add(db, v, 101, pk1, "p", start); t2 = add(db, v, 102, pk2, "p", start)
    r1 = run_until(rn, db, t1); r2 = run_until(rn, db, t2)
    assert r1["status"] == "minted" and r2["status"] == "minted", (dict(r1), dict(r2), msgs)
    assert fu.holds(a1.address) == 1 and fu.holds(a2.address) == 1
    assert {u for u, _ in msgs} == {101, 102}
    for tid, pk in ((t1, pk1), (t2, pk2)):
        log = open(rn.log_path(tid)).read()
        assert pk[2:] not in log and "k" * 32 not in log

def test_cancel_running_kills_child(tmp_path, env):
    ts, fo = env
    db, v, rn, msgs = mk(tmp_path, fo)
    a, pk = fu.funded_wallet()
    tid = add(db, v, 201, pk, "never", time.time() + 20)
    rn.tick(); t = db.task(tid)
    assert t["status"] == "running" and pid_is_task(t["pid"], tid)
    assert rn.cancel(201, tid)
    for _ in range(50):
        if not pid_is_task(t["pid"], tid): break
        time.sleep(0.1)
    assert not pid_is_task(t["pid"], tid) and db.task(tid)["status"] == "cancelled"
    assert rn.cancel(999, tid) is False

def test_expired_subscription_never_spawns(tmp_path, env):
    _, fo = env
    db, v, rn, msgs = mk(tmp_path, fo)
    a, pk = fu.funded_wallet()
    tid = add(db, v, 301, pk, "x", time.time() + 5, sub=False)
    rn.tick()
    t = db.task(tid)
    assert t["status"] == "failed" and t["pid"] is None and "اشتراک" in t["result"]

def test_restart_reattach(tmp_path, env):
    ts, fo = env
    db, v, rn, msgs = mk(tmp_path, fo)
    a, pk = fu.funded_wallet()
    cd = fu.signed_calldata(ts, a.address, 0xB4)
    fo.mint["ra"] = lambda m: (200, {"to": fu.SD, "data": cd, "value": "0", "chain": "robinhood"})
    tid = add(db, v, 401, pk, "ra", time.time() + 6)
    rn.tick(); assert db.task(tid)["status"] == "running"
    # simulate bot restart: brand-new Runner without the Popen handle
    rn2 = Runner(db, v, rn.cfg, rn.notify, extra_args=rn.extra_args)
    r = run_until(rn2, db, tid)
    assert r["status"] == "minted" and fu.holds(a.address) == 1
