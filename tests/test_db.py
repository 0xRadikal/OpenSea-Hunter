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
