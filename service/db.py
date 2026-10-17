"""SQLite storage. All secrets (wallet keys, OpenSea API keys) are stored only as vault ciphertext."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, created INTEGER NOT NULL,
  sub_until INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wallets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  address TEXT NOT NULL, blob BLOB NOT NULL, created INTEGER NOT NULL, UNIQUE(user_id, address));
CREATE TABLE IF NOT EXISTS apikeys(
  user_id INTEGER PRIMARY KEY REFERENCES users(id), blob BLOB NOT NULL,
  expires_at INTEGER, source TEXT NOT NULL, created INTEGER NOT NULL, last_ok INTEGER);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  wallet_id INTEGER NOT NULL REFERENCES wallets(id), slug TEXT NOT NULL, nft TEXT NOT NULL,
  stage_label TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
  status TEXT NOT NULL, pid INTEGER, log_offset INTEGER NOT NULL DEFAULT 0,
  tx_hash TEXT, result TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, stars INTEGER NOT NULL,
  payload TEXT NOT NULL, created INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, user_id INTEGER, action TEXT NOT NULL, detail TEXT);
"""
ACTIVE = ("scheduled", "running")
FINAL = ("minted", "failed", "cancelled", "not_eligible", "ended")


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            return cur.lastrowid

    def audit(self, uid, action, detail=""):
        self.x("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)", (int(time.time()), uid, action, detail[:500]))

    # users / subscriptions
    def ensure_user(self, uid, username=None):
        self.x("INSERT INTO users(id,username,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username",
               (uid, username, int(time.time())))
        return self.user(uid)

    def user(self, uid):
        r = self.q("SELECT * FROM users WHERE id=?", (uid,))
        return r[0] if r else None

    def is_subscribed(self, uid, now=None):
        u = self.user(uid)
        return bool(u) and not u["banned"] and u["sub_until"] > (now or time.time())

    def extend_sub(self, uid, days, now=None):
        """Extend from max(now, current expiry) so early renewals are never lost."""
        now = int(now or time.time())
        with self.lock:
            self.ensure_user(uid)
            cur = self.user(uid)["sub_until"]
            new = max(now, cur) + int(days * 86400)
            self.x("UPDATE users SET sub_until=? WHERE id=?", (new, uid))
            return new

    def record_payment(self, charge_id, uid, stars, payload, days):
        """Idempotent: a duplicate charge id (Telegram redelivery) never extends twice. Returns new expiry or None."""
        with self.lock:
            try:
                self.x("INSERT INTO payments(charge_id,user_id,stars,payload,created) VALUES(?,?,?,?,?)",
                       (charge_id, uid, stars, payload, int(time.time())))
            except sqlite3.IntegrityError:
                return None
            return self.extend_sub(uid, days)

    # wallets
    def add_wallet(self, uid, address, blob):
        with self.lock:
            self.x("INSERT INTO wallets(user_id,address,blob,created) VALUES(?,?,?,?) "
                   "ON CONFLICT(user_id,address) DO UPDATE SET blob=excluded.blob", (uid, address, blob, int(time.time())))
            return self.q("SELECT id FROM wallets WHERE user_id=? AND address=?", (uid, address))[0]["id"]

    def wallets(self, uid):
        return self.q("SELECT id,address,created FROM wallets WHERE user_id=? ORDER BY id", (uid,))

    def wallet(self, uid, wid):
        r = self.q("SELECT * FROM wallets WHERE user_id=? AND id=?", (uid, wid))
        return r[0] if r else None

    def delete_wallet(self, uid, wid):
        with self.lock:
            if self.q("SELECT 1 FROM tasks WHERE wallet_id=? AND status IN ('scheduled','running')", (wid,)):
                return False
            self.x("DELETE FROM tasks WHERE wallet_id=? AND user_id=?", (wid, uid))
            self.x("DELETE FROM wallets WHERE id=? AND user_id=?", (wid, uid))
            return True

    # api keys
    def set_apikey(self, uid, blob, expires_at, source):
        self.x("INSERT INTO apikeys(user_id,blob,expires_at,source,created,last_ok) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET blob=excluded.blob,expires_at=excluded.expires_at,"
               "source=excluded.source,created=excluded.created,last_ok=excluded.last_ok",
               (uid, blob, expires_at, source, int(time.time()), int(time.time())))

    def apikey(self, uid):
        r = self.q("SELECT * FROM apikeys WHERE user_id=?", (uid,))
        return r[0] if r else None

    # tasks
    def add_task(self, uid, wid, slug, nft, label, start, end):
        now = int(time.time())
        return self.x("INSERT INTO tasks(user_id,wallet_id,slug,nft,stage_label,start_ts,end_ts,status,created,updated) "
                      "VALUES(?,?,?,?,?,?,?,'scheduled',?,?)", (uid, wid, slug, nft.lower(), label, start, end, now, now))

    def task(self, tid):
        r = self.q("SELECT * FROM tasks WHERE id=?", (tid,))
        return r[0] if r else None

    def tasks(self, uid, active_only=False):
        sql = "SELECT * FROM tasks WHERE user_id=?" + (" AND status IN ('scheduled','running')" if active_only else "")
        return self.q(sql + " ORDER BY id DESC LIMIT 20", (uid,))

    def active_tasks(self):
        return self.q("SELECT * FROM tasks WHERE status IN ('scheduled','running') ORDER BY start_ts")

    def duplicate_task(self, uid, wid, nft, label):
        return bool(self.q("SELECT 1 FROM tasks WHERE user_id=? AND wallet_id=? AND nft=? AND stage_label=? "
                           "AND status IN ('scheduled','running')", (uid, wid, nft.lower(), label)))

    def update_task(self, tid, **kw):
        kw["updated"] = int(time.time())
        cols = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE tasks SET {cols} WHERE id=?", (*kw.values(), tid))
