"""OpenSea API client for the service layer (per-user API key; nothing shared between users).

Verified live 2026-10-17:
  * POST /api/v2/auth/keys -> instant key, 7-day expiry, limit "2 keys per day" PER IP (429 + retry-after).
    CORS echoes any Origin (also on 429), so the key is created from the USER's browser (keypage/), not the server.
  * SIWE: /auth/siwe/nonce -> /auth/siwe/verify (cookies) -> POST /auth/tokens {scopes:[read:eligibility]}
    -> PAT -> /auth/tokens/exchange -> wallet JWT (~1 h). First /auth/tokens call on a brand-new account can
    return 400 "Requested scopes exceed account entitlement"; it succeeded on the 2nd try 1.5 s later.
  * /drops/{slug}/eligibility needs BOTH X-API-KEY and the wallet JWT.
"""
import datetime as dt
import re
import time
from urllib.parse import urlparse

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

CHAIN = "robinhood"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SIWE_STATEMENT = ("Click to sign in and accept the Platform Terms of Service (https://opensea.io/tos) "
                  "and Privacy Policy (https://opensea.io/privacy).")


class OpenSeaError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def parse_link(text):
    """-> ("slug", slug) or ("contract", (chain, address)). Accepts bare slugs and opensea.io URLs."""
    t = (text or "").strip()
    if SLUG_RE.fullmatch(t.lower()) and "." not in t:
        return "slug", t.lower()
    if not re.match(r"^https?://", t):
        t = "https://" + t
    u = urlparse(t)
    host = (u.hostname or "").lower()
    if host not in ("opensea.io", "www.opensea.io"):
        raise OpenSeaError("فقط لینک opensea.io پذیرفته می‌شود")
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", parts[0]) and len(parts) > 1 and parts[1] in ("collection", "item", "assets"):
        parts = parts[1:]                                   # locale prefix /fa/collection/...
    if len(parts) >= 2 and parts[0] == "collection" and SLUG_RE.fullmatch(parts[1].lower()):
        return "slug", parts[1].lower()
    if len(parts) >= 3 and parts[0] in ("item", "assets") and ADDR_RE.fullmatch(parts[2]):
        return "contract", (parts[1].lower(), parts[2].lower())
    raise OpenSeaError("لینک کالکشن قابل تشخیص نیست (نمونه: https://opensea.io/collection/<slug>)")


def iso_ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


class OpenSea:
    def __init__(self, base="https://api.opensea.io", timeout=12):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _s(self):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
        return s

    def _get(self, path, key, jwt=None):
        h = {"X-API-KEY": key}
        if jwt:
            h["Authorization"] = "Bearer " + jwt
        try:
            r = self._s().get(self.base + path, headers=h, timeout=self.timeout)
        except requests.RequestException as e:
            raise OpenSeaError(f"network: {type(e).__name__}")
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:200]}
        if r.status_code != 200:
            err = body.get("errors") if isinstance(body, dict) else None
            raise OpenSeaError(f"HTTP {r.status_code}: {(err or [str(body)[:120]])[0]}", r.status_code)
        return body

    def test_key(self, key, slug="aerent"):
        """True if the key is accepted. 404 (unknown slug) still proves the key is valid."""
        if not re.fullmatch(r"[0-9A-Za-z_-]{16,128}", key or ""):
            return False, "فرمت کلید نامعتبر است"
        try:
            self._get(f"/api/v2/drops/{slug}", key)
            return True, "ok"
        except OpenSeaError as e:
            if e.status == 404:
                return True, "ok"
            if e.status == 401:
                return False, str(e)
            raise

    def resolve_slug(self, link, key):
        kind, v = parse_link(link)
        if kind == "slug":
            return v
        chain, addr = v
        if chain != CHAIN:
            raise OpenSeaError(f"فقط شبکه Robinhood پشتیبانی می‌شود (لینک روی {chain} است)")
        c = self._get(f"/api/v2/chain/{chain}/contract/{addr}", key)
        if not c.get("collection"):
            raise OpenSeaError("کالکشن این قرارداد پیدا نشد")
        return c["collection"]

    def drop(self, slug, key):
        return self._get(f"/api/v2/drops/{slug}", key)

    # ------------------------------------------------------------ wallet login (SIWE) + eligibility
    def wallet_jwt(self, private_key, tries=4):
        """Sign in to OpenSea with the wallet (SIWE), create a 1-day read:eligibility PAT, exchange it for a JWT,
        then revoke the PAT. Returns the JWT. Only a login message is signed: no transaction, no funds."""
        acct = Account.from_key(private_key)
        s = self._s()
        try:
            nonce = s.post(self.base + "/api/v2/auth/siwe/nonce", json={}, timeout=self.timeout).json()["nonce"]
            now = dt.datetime.now(dt.timezone.utc)
            iat = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
            msg = (f"opensea.io wants you to sign in with your Ethereum account:\n{acct.address}\n\n{SIWE_STATEMENT}\n\n"
                   f"URI: https://opensea.io\nVersion: 1\nChain ID: 1\nNonce: {nonce}\nIssued At: {iat}")
            sig = "0x" + acct.sign_message(encode_defunct(text=msg)).signature.hex().removeprefix("0x")
            r = s.post(self.base + "/api/v2/auth/siwe/verify", timeout=self.timeout, json={
                "message": {"domain": "opensea.io", "address": acct.address, "statement": SIWE_STATEMENT,
                            "uri": "https://opensea.io", "version": "1", "chainId": "1", "nonce": nonce,
                            "issuedAt": iat, "accountType": "Ethereum"},
                "signature": sig, "chainArch": "EVM"})
            if r.status_code != 200:
                raise OpenSeaError(f"SIWE verify HTTP {r.status_code}", r.status_code)
            pat = None
            for i in range(tries):
                r = s.post(self.base + "/api/v2/auth/tokens", timeout=self.timeout,
                           json={"label": "osh-eligibility", "scopes": ["read:eligibility"], "expiresInDays": 1})
                if r.status_code == 200:
                    pat = r.json()
                    break
                time.sleep(1.5 * (i + 1))
            if not pat or not pat.get("token"):
                raise OpenSeaError(f"PAT creation failed HTTP {r.status_code}", r.status_code)
            try:
                e = s.post(self.base + "/api/v2/auth/tokens/exchange", timeout=self.timeout,
                           json={"subjectToken": pat["token"], "subjectTokenType": "ACCESS_TOKEN"})
                jwt = e.json().get("accessToken") if e.status_code == 200 else None
            finally:
                try:
                    s.delete(self.base + f"/api/v2/auth/tokens/{pat['id']}", timeout=self.timeout)
                except Exception:
                    pass
            if not jwt:
                raise OpenSeaError("token exchange failed")
            return jwt
        except requests.RequestException as e:
            raise OpenSeaError(f"network: {type(e).__name__}")

    def eligibility(self, slug, key, jwt):
        """-> {stage_uuid: is_eligible}"""
        body = self._get(f"/api/v2/drops/{slug}/eligibility", key, jwt)
        return {str(st.get("stage_uuid")): bool(st.get("is_eligible")) for st in body.get("stages", [])}
