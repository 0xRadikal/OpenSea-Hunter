import time
from service.db import DB

def test_subscription_and_payments(tmp_path):
    d = DB(str(tmp_path / "x.db")); d.ensure_user(1, "a")
    assert not d.is_subscribed(1)
    e1 = d.record_payment("ch1", 1, 500, "sub30", 30)
    assert e1 and d.is_subscribed(1)
    assert d.record_payment("ch1", 1, 500, "sub30", 30) is None        # redelivery: no double extension
    e2 = d.record_payment("ch2", 1, 500, "sub30", 30)
    assert abs((e2 - e1) - 30 * 86400) <= 1                              # early renewal stacks
    d.x("UPDATE users SET banned=1 WHERE id=1"); assert not d.is_subscribed(1)

def test_expiry_extends_from_now(tmp_path):
    d = DB(str(tmp_path / "x.db")); d.ensure_user(2)
    d.x("UPDATE users SET sub_until=? WHERE id=2", (int(time.time()) - 10 * 86400,))
    new = d.extend_sub(2, 1)
    assert abs(new - (time.time() + 86400)) < 5

def test_wallet_task_rules(tmp_path):
    d = DB(str(tmp_path / "x.db")); d.ensure_user(1); d.ensure_user(2)
    w = d.add_wallet(1, "0xAbc", b"blob")
    assert d.add_wallet(1, "0xAbc", b"blob2") == w                       # upsert, same id
    assert d.wallet(2, w) is None                                        # other user cannot see it
    t = d.add_task(1, w, "s", "0xNFT", "OG", 1, 2)
    assert d.duplicate_task(1, w, "0xnft", "OG")
    assert d.delete_wallet(1, w) is False                                # active task blocks delete
    d.update_task(t, status="minted")
    assert not d.duplicate_task(1, w, "0xnft", "OG")
    assert d.delete_wallet(1, w) is True and d.wallets(1) == []


def test_policy_columns_and_atomic(tmp_path):
    import threading, pytest
    d = DB(str(tmp_path / "x.db")); d.ensure_user(1); w = d.add_wallet(1, "0xa", b"b")
    t = d.add_task(1, w, "s", "0xN", "Public", 1, 2, "public", 10 ** 15, 10 ** 15)
    r = d.task(t); assert (r["kind"], r["price_wei"], r["max_value_wei"]) == ("public", str(10 ** 15), str(10 ** 15))
    for bad in (("weird", 0, 0), ("public", 5, 4), ("signed", -1, 0)):
        with pytest.raises(ValueError): d.add_task(1, w, "s", "0xM", "X", 1, 2, *bad)
    out = []
    ts = [threading.Thread(target=lambda: out.append(d.add_task_once(1, w, "s", "0xZ", "OG", 1, 2, 9))) for _ in range(20)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert sum(isinstance(x, int) for x in out) == 1
    assert d.add_task_once(1, w, "s", "0xY", "OG", 1, 2, 2) == "limit"


def test_migration_of_old_db(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db"); c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, wallet_id INTEGER NOT NULL,
      slug TEXT NOT NULL, nft TEXT NOT NULL, stage_label TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
      status TEXT NOT NULL, pid INTEGER, log_offset INTEGER NOT NULL DEFAULT 0, tx_hash TEXT, result TEXT,
      created INTEGER NOT NULL, updated INTEGER NOT NULL);
      INSERT INTO tasks(user_id,wallet_id,slug,nft,stage_label,start_ts,end_ts,status,created,updated) VALUES(1,1,'s','0xn','OG',1,2,'minted',1,1);""")
    c.commit(); c.close()
    d = DB(p); r = d.task(1)
    assert (r["kind"], r["price_wei"], r["max_value_wei"]) == ("signed", "0", "0")


def test_add_plan_atomic(tmp_path):
    from service.db import DB
    db = DB(str(tmp_path / "p.db")); db.ensure_user(1); wid = db.add_wallet(1, "0x" + "a" * 40, b"x")
    it = lambda l, q, p=0, k="signed": {"label": l, "start": 100, "end": 200, "kind": k, "price_wei": p, "quantity": q}
    ids = db.add_plan(1, wid, "s", "0x" + "B" * 40, [it("AL", 3), it("Pub", 10, 10**15, "public")], 3, "pid1")
    assert isinstance(ids, list) and len(ids) == 2
    t = [db.task(i) for i in ids]
    assert (t[0]["quantity"], t[0]["max_value_wei"], t[1]["quantity"], t[1]["max_value_wei"]) == (3, "0", 10, str(10 ** 16))
    assert t[0]["plan_id"] == t[1]["plan_id"] == "pid1" and t[0]["nft"] == "0x" + "b" * 40
    assert db.add_plan(1, wid, "s", "0x" + "b" * 40, [it("Other", 1), it("AL", 1)], 9, "p2") == "dup"      # nothing written
    assert db.add_plan(1, wid, "s", "0x" + "b" * 40, [it("X", 1), it("Y", 1)], 3, "p3") == "limit"
    assert len(db.tasks(1)) == 2
    import pytest
    with pytest.raises(ValueError):
        db.add_plan(1, wid, "s", "0x" + "c" * 40, [it("A", 1), it("B", 101)], 9, "p4")                    # qty > 100
    with pytest.raises(ValueError):
        db.add_plan(1, wid, "s", "0x" + "c" * 40, [it("A", 0)], 9, "p5")
    assert len(db.tasks(1)) == 2                                                                         # all-or-nothing


def test_migration_adds_quantity(tmp_path):
    import sqlite3
    from service.db import DB
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, wallet_id INTEGER NOT NULL,"
                    " slug TEXT NOT NULL, nft TEXT NOT NULL, stage_label TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,"
                    " status TEXT NOT NULL, pid INTEGER, log_offset INTEGER NOT NULL DEFAULT 0, tx_hash TEXT, result TEXT,"
                    " created INTEGER NOT NULL, updated INTEGER NOT NULL);"
                    "INSERT INTO tasks(user_id,wallet_id,slug,nft,stage_label,start_ts,end_ts,status,created,updated)"
                    " VALUES(1,1,'s','0x',  'L',1,2,'minted',1,1);")
    c.commit(); c.close()
    db = DB(p)
    r = db.task(1)
    assert r["quantity"] == 1 and r["kind"] == "signed" and r["plan_id"] is None


def test_key_pool_assignment(tmp_path):
    from service.db import DB
    db = DB(str(tmp_path / "k.db")); db.ensure_user(1); wid = db.add_wallet(1, "0x" + "a" * 40, b"x")
    now = 10_000
    k1 = db.add_apikey(1, b"b1", now + 7 * 86400, "t", "f1")
    k2 = db.add_apikey(1, b"b2", now + 7 * 86400, "t", "f2")
    assert db.add_apikey(1, b"b1", now, "t", "f1") == "dup"
    it = lambda s, e: {"start": s, "end": e}
    # two overlapping stages -> two different keys
    a = db.pick_keys(1, [it(now + 100, now + 900), it(now + 200, now + 900)])
    assert {a[0][0], a[1][0]} == {k1, k2} and not a[0][1] and not a[1][1]
    # third overlapping stage -> shared (flagged)
    a = db.pick_keys(1, [it(now + 100, now + 900)] * 3)
    assert a[2][1] is True and a[2][0] in (k1, k2)
    # non-overlapping stages may reuse a key without being flagged
    a = db.pick_keys(1, [it(now + 100, now + 200), it(now + 5000, now + 6000)])
    assert not a[0][1] and not a[1][1]
    # key that expires before the stage is never picked
    k3 = db.add_apikey(1, b"b3", now + 50, "t", "f3")
    for kid, _ in db.pick_keys(1, [it(now + 100, now + 900)] * 3):
        assert kid != k3
    # active tasks in the DB count as load
    ids = db.add_plan(1, wid, "s", "0x" + "b" * 40,
                      [{"label": "A", "start": now + 100, "end": now + 900, "kind": "signed", "price_wei": 0, "quantity": 1},
                       {"label": "B", "start": now + 100, "end": now + 900, "kind": "public", "price_wei": 0, "quantity": 1}], 20, "p")
    assert {db.task(i)["apikey_id"] for i in ids} == {k1, k2}
    assert db.delete_apikey(1, k1) is False                       # used by an active task
    assert db.delete_apikey(1, k3) is True


def test_legacy_key_migrated(tmp_path):
    import sqlite3
    from service.db import DB
    p = str(tmp_path / "l.db")
    DB(p)                                                         # create schema
    c = sqlite3.connect(p)
    c.execute("INSERT INTO users(id,created) VALUES(5,1)")
    c.execute("INSERT INTO apikeys(user_id,blob,expires_at,source,created) VALUES(5,?,?,?,1)", (b"legacy", 99, "manual"))
    c.commit(); c.close()
    db = DB(p); db2 = DB(p)                                       # migration is idempotent
    ks = db2.apikeys(5)
    assert len(ks) == 1 and bytes(ks[0]["blob"]) == b"legacy" and ks[0]["expires_at"] == 99
    assert db.apikey(5)["id"] == ks[0]["id"]


def test_prefs_settings_and_policy_columns(tmp_path):
    import pytest
    from service.db import DB
    db = DB(str(tmp_path / "s.db")); db.ensure_user(1); wid = db.add_wallet(1, "0x" + "a" * 40, b"x")
    assert db.prefs(1)["price_policy"] == "exact"
    db.set_prefs(1, "cap", 25); assert db.prefs(1) == {"user_id": 1, "price_policy": "cap", "max_increase_pct": 25}
    with pytest.raises(ValueError):
        db.set_prefs(1, "yolo", 1)
    assert db.max_active(20) == 20
    db.set_setting("max_active_tasks", 77); assert db.max_active(20) == 77
    P = 10 ** 15
    ids = db.add_plan(1, wid, "s", "0x" + "b" * 40, [{"label": "A", "start": 1, "end": 2, "kind": "public", "price_wei": P,
                                                     "quantity": 3, "policy": "cap", "min_unit": P, "max_unit": P * 5 // 4}], 20, "p")
    t = db.task(ids[0])
    assert (t["min_unit_wei"], t["max_unit_wei"], t["max_value_wei"], t["price_policy"]) == (str(P), str(P * 5 // 4), str(3 * P * 5 // 4), "cap")
    for i in range(30):
        db.add_task(1, wid, "s", "0x" + "c" * 40, f"L{i}", 1, 2)
    assert db.active_count(1) == 31                               # not capped at 20 like tasks()


def test_internal_ensure_user_keeps_username(tmp_path):
    """Regression: extend_sub/grant/payments call ensure_user(uid) without a username; that must not erase it."""
    from service.db import DB
    d = DB(str(tmp_path / "u.db"))
    d.ensure_user(1, "alice")
    d.extend_sub(1, 30)
    assert d.user(1)["username"] == "alice"
    d.ensure_user(1, "bob")
    assert d.user(1)["username"] == "bob"
