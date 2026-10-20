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
