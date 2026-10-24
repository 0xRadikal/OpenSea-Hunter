"""Regressions for the bugs found in the 2026-10-21 audit (each was reproduced against the real code first).

1. cancel vs spawn race: a task cancelled after the runner's tick snapshot was resurrected to "running" + engine started
2. _finish after cancel overwrote "cancelled" and sent an extra notification
3. Stars payment: payment row inserted but extend_sub failed -> Telegram redelivery returned None, never subscribed
4. edited messages crashed the text handler (u.message is None)
5. pasted-key detection outside the flow missed whitespace / quote variants
6. wallet deleted between the plan summary and «تأیید» -> FK IntegrityError, plan stuck in "arming"
7. parse_key_text crashed on non-list errors / numeric expires_at
8. check_drop crashed if OpenSea sends supply as int
"""
import asyncio, os, re, threading, time

import pytest

from service.config import Config
from service.db import DB
from service.runner import Runner
from service.vault import Vault, create_master_key, load_master_key


def _world(tmp_path):
    cfg = Config(); cfg.data_dir = str(tmp_path); cfg.master_key_file = str(tmp_path / "k")
    create_master_key(cfg.master_key_file); os.makedirs(cfg.log_dir, exist_ok=True)
    db = DB(str(tmp_path / "t.db")); v = Vault(load_master_key(cfg.master_key_file))
    db.ensure_user(1); db.extend_sub(1, 3)
    addr, blob = v.encrypt(1, "0x" + "11" * 32); w = db.add_wallet(1, addr, blob)
    kid = db.add_apikey(1, v.seal(1, "apikey", "k" * 32), int(time.time()) + 7 * 86400, "manual", "fp")
    return cfg, db, v, w, kid


# ---------------------------------------------------------------- 1/2 runner state machine
def test_transition_is_compare_and_set(tmp_path):
    _, db, _, w, _ = _world(tmp_path)
    t = db.add_task(1, w, "s", "0x" + "2" * 40, "L", 1, 2)
    assert db.transition(t, ("scheduled",), status="running") is True
    assert db.transition(t, ("scheduled",), status="running") is False        # already moved
    assert db.transition(t, ("scheduled", "running"), status="cancelled") is True
    assert db.transition(t, ("scheduled", "running"), status="minted") is False
    assert db.task(t)["status"] == "cancelled"


def test_spawn_with_stale_snapshot_after_cancel_starts_nothing(tmp_path):
    cfg, db, v, w, kid = _world(tmp_path)
    now = int(time.time())
    tid = db.add_task(1, w, "s", "0x" + "2" * 40, "L", now + 30, now + 3600, "public", 0, 0, 1, None, kid)
    r = Runner(db, v, cfg, notify=lambda *a, **k: None)
    snap = db.task(tid)                                   # tick() snapshot
    assert r.cancel(1, tid) is True
    r.spawn(snap)                                         # tick continues with the stale snapshot
    t = db.task(tid)
    assert t["status"] == "cancelled" and t["pid"] is None and tid not in r.procs
    assert not os.path.exists(r.log_path(tid))            # engine was never started


def test_finish_after_cancel_keeps_cancelled_and_is_silent(tmp_path):
    cfg, db, v, w, _ = _world(tmp_path)
    now = int(time.time())
    tid = db.add_task(1, w, "s", "0x" + "2" * 40, "L", now - 7200, now - 10, "public", 0, 0, 1)
    sent = []
    r = Runner(db, v, cfg, notify=lambda *a, **k: sent.append(a), admin_notify=lambda *a: sent.append(a))
    snap = db.task(tid)
    r.cancel(1, tid)
    r._finish(snap, "ended", "stage ended")
    assert db.task(tid)["status"] == "cancelled" and sent == []


def test_cancelled_task_that_still_minted_keeps_tx(tmp_path):
    cfg, db, v, w, _ = _world(tmp_path)
    tid = db.add_task(1, w, "s", "0x" + "2" * 40, "L", 1, 2)
    r = Runner(db, v, cfg, notify=lambda *a, **k: None)
    snap = db.task(tid); r.cancel(1, tid)
    with open(r.log_path(tid), "w") as f:
        f.write('OSH_RESULT {"minted": true, "tx": "0xabc", "token_ids": [5]}\n')
    r._finish(snap, "minted", "ok")
    t = db.task(tid)
    assert t["status"] == "cancelled" and t["tx_hash"] == "0xabc" and t["result"].startswith("minted after cancel")


# ---------------------------------------------------------------- 3 Stars atomicity
def test_record_payment_is_atomic(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "x.db")); db.ensure_user(1)
    real = db.extend_sub
    monkeypatch.setattr(db, "extend_sub", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        db.record_payment("ch", 1, 500, "sub:30:1", 30)
    assert db.q("SELECT COUNT(*) FROM payments")[0][0] == 0          # rolled back -> redelivery can succeed
    monkeypatch.setattr(db, "extend_sub", real)
    assert db.record_payment("ch", 1, 500, "sub:30:1", 30) and db.is_subscribed(1)
    assert db.record_payment("ch", 1, 500, "sub:30:1", 30) is None   # still idempotent


# ---------------------------------------------------------------- 6 wallet deleted before arming
def test_add_plan_with_deleted_wallet(tmp_path):
    _, db, _, w, _ = _world(tmp_path)
    assert db.delete_wallet(1, w)
    it = [{"label": "A", "start": time.time() + 999, "end": time.time() + 9999, "kind": "signed", "price_wei": 0, "quantity": 1}]
    assert db.add_plan(1, w, "s", "0x" + "b" * 40, it, 10, "p") == "nowallet"
    assert db.add_plan(2, w, "s", "0x" + "b" * 40, it, 10, "p") == "nowallet"   # other user's wallet id
    assert db.q("SELECT COUNT(*) FROM tasks")[0][0] == 0


# ---------------------------------------------------------------- 7 / 8 parsers
def test_parse_key_text_never_raises():
    from service.bot import Service
    P = Service.parse_key_text
    assert P('{"errors":["API key has expired"]}') == ("ERR:API key has expired", None)
    assert P('{"errors":{"x":1}}')[0].startswith("ERR:")
    assert P('{"errors":"boom"}') == ("ERR:boom", None)
    assert P('{"api_key":"k","expires_at":1792000000}') == ("k", 1792000000)
    assert P('{"api_key":"k","expires_at":"2026-10-28T10:00:00Z"}') == ("k", 1793181600)
    assert P('{"api_key":"k","expires_at":true}') == ("k", None)
    assert P('{"api_key":"k","expires_at":"bad"}') == ("k", None)
    assert P("{bad") == (None, None) and P(None) == ("", None) and P("  abc ") == ("abc", None)


def test_check_drop_supply_str_or_int():
    from service import preflight as pf
    base = {"chain": "robinhood", "contract_address": "0x" + "a" * 40}
    for ms, ts in (("1000", "50"), (1000, 50), (None, None), (0, 0), (1000, None)):
        pf.check_drop(dict(base, max_supply=ms, total_supply=ts))
    for ms, ts in (("1000", "1000"), (1000, 1000), (10, 11)):
        with pytest.raises(pf.PreflightError):
            pf.check_drop(dict(base, max_supply=ms, total_supply=ts))


# ---------------------------------------------------------------- 4 / 5 bot handlers (fake Telegram)
@pytest.fixture(scope="module")
def bot(tmp_path_factory):
    from tests.fake_telegram import FakeTelegram
    import service.bot as botmod
    tg = FakeTelegram(); d = tmp_path_factory.mktemp("audit")
    cfg = Config(); cfg.data_dir = str(d / "data"); cfg.master_key_file = str(d / "m.key"); create_master_key(cfg.master_key_file)
    cfg.bot_token = "123456:TEST"; cfg.telegram_base_url = tg.url
    svc = botmod.Service(cfg); app = svc.build(); loop = asyncio.new_event_loop()

    async def boot():
        await app.initialize(); await svc._post_init(app)
        await app.updater.start_polling(poll_interval=0.05, timeout=1); await app.start()
    threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(boot()), loop.run_forever()),
                     daemon=True).start()
    time.sleep(1.5)
    yield tg, svc

    async def down():
        await app.updater.stop(); await app.stop(); await svc._post_shutdown(app); await app.shutdown()
    asyncio.run_coroutine_threadsafe(down(), loop).result(20)
    loop.call_soon_threadsafe(loop.stop); tg.close()


def _edit(tg, uid, text, mid=5):
    tg._push({"edited_message": {"message_id": mid, "date": int(time.time()), "edit_date": int(time.time()),
                                 "chat": {"id": uid, "type": "private"}, "from": tg.user(uid), "text": text}})


def _calls(tg, m):
    time.sleep(1.2)
    return [c for c in tg.calls[m:] if c[0] not in ("getUpdates",)]


def test_edited_message_is_ignored_and_does_not_crash(bot):
    tg, svc = bot
    m = tg.mark(); _edit(tg, 77, "hello there")
    calls = _calls(tg, m)
    assert calls == [], calls                                 # before the fix: error card «مشکلی پیش آمد»


def test_edited_message_with_key_is_deleted(bot):
    tg, svc = bot
    m = tg.mark(); _edit(tg, 77, "  0x" + "ab" * 32 + "\n", mid=9)
    calls = _calls(tg, m)
    assert any(c[0] == "deleteMessage" and str(c[1]["message_id"]) == "9" for c in calls), calls


@pytest.mark.parametrize("variant", ["ab" * 32, "0x" + "AB" * 32, "  " + "ab" * 32 + "  ", '"' + "ab" * 32 + '"',
                                     "'0X" + "cd" * 32 + "'", "ab" * 32 + "\n"])
def test_pasted_key_outside_flow_is_deleted(bot, variant):
    tg, svc = bot
    m = tg.mark(); mid = tg.text(78, variant)
    calls = _calls(tg, m)
    assert any(c[0] == "deleteMessage" and str(c[1]["message_id"]) == str(mid) for c in calls), (variant, calls)


def test_key_regex_does_not_eat_normal_text():
    rx = r"(0[xX])?[0-9a-fA-F]{64}"
    for t in ("ab" * 31, "ab" * 33, "0x" + "ab" * 31, "hello", "g" * 64):
        assert not re.fullmatch(rx, t.strip().strip('"').strip("'"))
