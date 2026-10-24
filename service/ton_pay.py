"""TON payment gateway — pay from ANY wallet or exchange, verified automatically on-chain.

How a payment is attributed to an invoice (verified against TonAPI v2 and real chain data, 2026-10-21):
  1. Comment (memo): every invoice has a unique code `RMxxxxxxxx`; any incoming transfer whose text comment contains
     it is credited to that invoice (several partial transfers add up; payment after expiry still counts).
  2. Unique amount: every pending invoice also has a unique amount (price + 0.0001..0.0999 TON). A transfer WITHOUT a
     usable memo (e.g. an exchange that cannot attach a comment) is credited when its value equals exactly one
     pending, unexpired invoice amount.
  Anything else is recorded as "unmatched" and reported to the admins — never credited by guesswork.

Safety rules for an incoming transfer (all must hold): ordinary transaction, success and not aborted, inbound internal
message, not a bounce, destination == our address, positive TON value. Every transaction hash is processed once
(PRIMARY KEY), crediting + subscription extension + cursor update happen in ONE SQLite transaction.
"""
import asyncio
import base64
import logging
import re
import secrets
import time
import urllib.parse
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger("ton")

NANO = 10 ** 9
OFFSET_STEP = 10 ** 5            # 0.0001 TON
OFFSET_MAX = 999                 # unique part of the amount: 0.0001 .. 0.0999 TON
INVOICE_TTL = 3600               # unique-amount matching window (memo matching never expires)
MEMO_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no 0/O/1/I (easy to type from a screen)
MEMO_RE = re.compile(r"RM[2-9A-HJ-NP-Z]{8}")


# ------------------------------------------------------------------ addresses (TEP-2)
def _crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def parse_address(s):
    """-> (workchain, 32-byte hash, info dict). Accepts raw `wc:hex` or user-friendly base64/base64url (CRC checked)."""
    s = (s or "").strip()
    m = re.fullmatch(r"(-?\d+):([0-9a-fA-F]{64})", s)
    if m:
        return int(m.group(1)), bytes.fromhex(m.group(2)), {"form": "raw"}
    if len(s) != 48:
        raise ValueError("bad address length")
    try:
        b = base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_"))
    except (ValueError, TypeError):
        raise ValueError("bad address encoding")
    if len(b) != 36 or _crc16(b[:34]).to_bytes(2, "big") != b[34:]:
        raise ValueError("bad address checksum")
    tag = b[0]
    if tag & 0x7F not in (0x11, 0x51):
        raise ValueError("bad address tag")
    wc = b[1] - 256 if b[1] > 127 else b[1]
    return wc, b[2:34], {"form": "friendly", "bounceable": tag & 0x7F == 0x11, "testnet": bool(tag & 0x80)}


def raw_address(s):
    wc, h, _ = parse_address(s)
    return f"{wc}:{h.hex()}"


def friendly_address(s, bounceable=False):
    wc, h, _ = parse_address(s)
    x = bytes([0x11 if bounceable else 0x51, wc & 0xFF]) + h
    return base64.urlsafe_b64encode(x + _crc16(x).to_bytes(2, "big")).decode()


# ------------------------------------------------------------------ amounts / memo
def fmt_ton(nano):
    """nano -> '3.5037' (no scientific notation, trailing zeros trimmed, at least one decimal)."""
    s = f"{Decimal(int(nano)) / NANO:.9f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def parse_ton(s):
    """'3.5' / '3,5' / '۳.۵' -> nano (int > 0). Raises ValueError."""
    t = str(s).strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫,", "01234567890123456789..")).replace(" ", "")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,9})?", t):
        raise ValueError("bad TON amount")
    try:
        n = int(Decimal(t) * NANO)
    except InvalidOperation:
        raise ValueError("bad TON amount")
    if n <= 0:
        raise ValueError("amount must be positive")
    return n


def new_memo():
    return "RM" + "".join(secrets.choice(MEMO_CHARS) for _ in range(8))


def extract_memo(text):
    """Find an invoice code in a free-text comment (case/spacing/dashes tolerant)."""
    if not text:
        return None
    norm = re.sub(r"[^0-9A-Z]", "", str(text).upper())
    m = MEMO_RE.search(norm)
    return m.group(0) if m else None


def incoming(tx, our_raw):
    """TonAPI v2 Transaction -> credited inbound TON transfer dict, or None if it must not count as a payment."""
    try:
        if tx.get("transaction_type") != "TransOrd" or not tx.get("success") or tx.get("aborted"):
            return None
        im = tx.get("in_msg") or {}
        if im.get("msg_type") != "int_msg" or im.get("bounced"):
            return None
        if raw_address((im.get("destination") or {}).get("address", "")) != our_raw:
            return None
        if raw_address((tx.get("account") or {}).get("address", "")) != our_raw:
            return None
        value = int(im.get("value") or 0)
        if value <= 0:
            return None
        comment = None
        if im.get("decoded_op_name") == "text_comment" and isinstance(im.get("decoded_body"), dict):
            comment = str(im["decoded_body"].get("text") or "")[:500]
        # Tonkeeper & co. can send an ENCRYPTED comment (op 0x2167da4b, seen live 2026-10-21 on a real payment):
        # only the receiver's private key can read it, so it is treated as "no memo" (unique-amount matching).
        encrypted = im.get("decoded_op_name") == "encrypted_text_comment" or str(im.get("op_code") or "").lower() == "0x2167da4b"
        src = (im.get("source") or {}).get("address")
        return {"hash": str(tx["hash"]).lower(), "lt": int(tx["lt"]), "utime": int(tx["utime"]), "value": value,
                "comment": comment, "memo": extract_memo(comment), "source": src, "encrypted": encrypted}
    except (KeyError, TypeError, ValueError):
        return None


def transfer_links(address, amount_nano, memo):
    """Payment deep links (formats per docs.ton.org/onboarding/wallet-apps/deep-links and Tonkeeper docs)."""
    q = urllib.parse.urlencode({"amount": str(int(amount_nano)), "text": memo})
    return {"ton": f"ton://transfer/{address}?{q}",
            "tonkeeper": f"https://app.tonkeeper.com/transfer/{address}?{q}",
            "tonhub": f"https://tonhub.com/transfer/{address}?{q}"}


def qr_png(data, scale=8):
    import io
    import segno
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=scale, border=4, dark="#111111", light="#ffffff")
    return buf.getvalue()


def tx_url(tx_hash):
    return f"https://tonviewer.com/transaction/{tx_hash}"


# ------------------------------------------------------------------ TonAPI client
class TonApiError(Exception):
    pass


class TonApi:
    def __init__(self, key, base="https://tonapi.io", timeout=15):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {key}"} if key else {}
        self.timeout = timeout
        self._rate = (0.0, None)

    async def _get(self, path, params=None):
        delay = 1.0
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.get(self.base + path, params=params, headers=self.h)
            except httpx.HTTPError as ex:
                err = f"network: {type(ex).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                err = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise TonApiError(err)                     # 4xx other than 429: retrying will not help
            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2
        raise TonApiError(err)

    async def transactions_after(self, account_raw, after_lt, limit=100):
        """Ascending by lt, strictly after `after_lt` (verified live: after_lt is exclusive)."""
        d = await self._get(f"/v2/blockchain/accounts/{account_raw}/transactions",
                            {"after_lt": int(after_lt), "sort_order": "asc", "limit": limit})
        return d.get("transactions") or []

    async def latest(self, account_raw, limit=100):
        d = await self._get(f"/v2/blockchain/accounts/{account_raw}/transactions", {"sort_order": "desc", "limit": limit})
        return d.get("transactions") or []

    async def usd_rate(self):
        """TON -> USD (cached 10 min); None if unavailable. Display only, never used for crediting."""
        ts, v = self._rate
        if time.time() - ts < 600 and v:
            return v
        try:
            d = await self._get("/v2/rates", {"tokens": "ton", "currencies": "usd"})
            v = float(d["rates"]["TON"]["prices"]["USD"])
            self._rate = (time.time(), v)
            return v
        except (TonApiError, KeyError, TypeError, ValueError):
            return v


# ------------------------------------------------------------------ gateway
class TonGateway:
    def __init__(self, db, cfg, on_event=None):
        self.db, self.cfg = db, cfg
        self.on_event = on_event or (lambda kind, data: None)     # bot notifications (paid / partial / unmatched)
        self.address = self.raw = None
        if cfg.ton_address:
            try:
                wc, _, info = parse_address(cfg.ton_address)
                if info.get("testnet") or wc != 0:
                    raise ValueError("testnet or non-basechain address")
                self.address, self.raw = cfg.ton_address.strip(), raw_address(cfg.ton_address)
            except ValueError as ex:
                log.error("TON gateway disabled: invalid TON_ADDRESS (%s)", ex)
        self.api = TonApi(cfg.ton_api_key, cfg.ton_api_base)
        self._poll_lock = asyncio.Lock()
        self._down_since, self._down_alerted = None, False
        self._last_poll = 0.0
        self.healthy = None

    # -- configuration
    def price_nano(self):
        v = self.db.setting("ton_price_nano")
        if v is None and self.cfg.price_ton:
            try:
                return parse_ton(self.cfg.price_ton)
            except ValueError:
                return None
        return int(v) if v and int(v) > 0 else None

    def enabled(self):
        return bool(self.raw and self.cfg.ton_api_key and self.price_nano())

    # -- invoices
    def invoice_for(self, uid, now=None):
        now = int(now or time.time())
        price = self.price_nano()
        if not price:
            raise RuntimeError("TON price not configured")
        inv = self.db.ton_invoice_for(uid, price, self.cfg.sub_days, now, INVOICE_TTL, new_memo, OFFSET_STEP, OFFSET_MAX)
        self.wake()
        return inv

    # -- verification
    async def poll_once(self, force=False):
        """Fetch new transactions after the stored cursor and apply them. Returns number of credited events."""
        if not self.raw or not self.cfg.ton_api_key:
            return 0
        async with self._poll_lock:
            if force and time.time() - self._last_poll < 3:
                return 0
            self._last_poll = time.time()
            n = 0
            try:
                cur = self.db.setting("ton_last_lt")
                if cur is None:
                    # first run: start at the newest transaction; still apply the recent ones that could belong to an
                    # invoice that already exists (dedupe by tx hash makes this safe)
                    txs = sorted(await self.api.latest(self.raw), key=lambda t: int(t["lt"]))
                    since = self.db.ton_oldest_open_invoice()
                    for t in txs:
                        if since is not None and int(t.get("utime", 0)) >= since - 60:
                            n += self._apply(t)
                        else:                                     # pre-existing history: remember, never credit
                            inc = incoming(t, self.raw)
                            if inc:
                                self.db.ton_record_history(inc, int(time.time()))
                    self.db.set_setting("ton_last_lt", max([int(t["lt"]) for t in txs], default=0))
                else:
                    cur = int(cur)
                    for _ in range(20):                                   # up to 2000 tx per poll
                        txs = await self.api.transactions_after(self.raw, cur)
                        for t in sorted(txs, key=lambda t: int(t["lt"])):
                            n += self._apply(t)
                            cur = max(cur, int(t["lt"]))
                            self.db.set_setting("ton_last_lt", cur)
                        if len(txs) < 100:
                            break
                self._set_health(True, None)
            except (TonApiError, KeyError, TypeError, ValueError) as ex:
                self._set_health(False, str(ex))
                log.warning("TON poll failed: %s", ex)
            return n

    OUTAGE_ALERT_S = 300                         # admins are told after 5 min of failures (not on every blip)

    def _set_health(self, ok, err):
        """Track API health; emit 'api_down' once after OUTAGE_ALERT_S of continuous failures and 'api_up' once
        when it recovers after such an alert."""
        now = time.time()
        if ok:
            if getattr(self, "_down_alerted", False):
                self.on_event("api_up", {"since": self._down_since, "secs": now - self._down_since})
            self._down_since, self._down_alerted = None, False
        else:
            if getattr(self, "_down_since", None) is None:
                self._down_since = now
            if not getattr(self, "_down_alerted", False) and now - self._down_since >= self.OUTAGE_ALERT_S:
                self._down_alerted = True
                self.on_event("api_down", {"since": self._down_since, "error": err})
        self.healthy = ok

    def _apply(self, tx):
        inc = incoming(tx, self.raw)
        if not inc:
            return 0
        res = self.db.ton_apply(inc, int(time.time()))
        kind = res[0]
        if kind == "dup":
            return 0
        log.info("TON %s: %s nano tx %s", kind, inc["value"], inc["hash"])
        self.on_event(kind, {"inc": inc, **res[1]})
        return 1

    POLL_BUSY, POLL_IDLE = 10, 60                # seconds; busy = an invoice opened in the last 24 h

    async def loop(self):
        """Poll every POLL_BUSY s while invoices are open, else every POLL_IDLE s. A new invoice wakes the loop
        immediately (wake()), so the fast cadence starts the moment a user asks to pay."""
        self._wake = asyncio.Event()
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 — the poller must never die
                log.exception("TON loop error")
            busy = self.db.ton_has_open_invoices(int(time.time()) - 86400)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL_BUSY if busy else self.POLL_IDLE)
            except asyncio.TimeoutError:
                pass

    def wake(self):
        ev = getattr(self, "_wake", None)
        if ev is not None:
            ev.set()
