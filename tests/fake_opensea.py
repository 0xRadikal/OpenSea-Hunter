"""Minimal fake of the OpenSea endpoints the service + engine use (local HTTP, tests only)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenSea:
    def __init__(self):
        self.drops = {}          # slug -> drop json
        self.mint = {}           # slug -> callable(minter) -> (code, body)
        self.active_after = {}   # slug -> ts when active_stage label appears
        self.calls = []
        self.mint_bodies = []    # every POST /mint body (minter, quantity)
        self.valid_keys = {"k" * 32}
        self.eligible = set()    # lower-case addresses eligible for every stage
        self.caps = {}           # (slug, stage_uuid) -> int | fn(addr) : max_total_mintable_by_wallet (absolute)
        self.contracts = {}      # (chain, addr) -> slug
        self.nonces, self.sessions, self.pats, self.jwts = set(), {}, {}, {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _auth(self):
                return self.headers.get("X-API-KEY") in outer.valid_keys

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if not self._auth():
                    return self._send(401, {"errors": ["Invalid API key"]})
                p = self.path.split("?")[0].strip("/").split("/")
                if p[:3] == ["api", "v2", "drops"] and len(p) == 4:
                    d = outer.drops.get(p[3])
                    if not d:
                        return self._send(404, {"errors": ["not found"]})
                    d = dict(d)
                    t = outer.active_after.get(p[3])
                    if t is not None:
                        d["active_stage"] = ({"label": d["stages"][0]["label"]} if time.time() >= t else None)
                    return self._send(200, d)
                if p[:3] == ["api", "v2", "drops"] and len(p) == 5 and p[4] == "eligibility":
                    addr = outer.jwts.get(self.headers.get("Authorization", "")[7:])
                    if not addr:
                        return self._send(401, {"errors": ["auth"]})
                    d = outer.drops.get(p[3], {})
                    out = []
                    for s in d.get("stages", []):
                        e = {"stage_uuid": s["uuid"], "is_eligible": addr in outer.eligible or s.get("stage_type") == "public_sale",
                             "price": s.get("price"), "max_total_mintable_by_wallet_per_token": None}
                        cf = outer.caps.get((p[3], s["uuid"]))
                        e["max_total_mintable_by_wallet"] = (str(cf(addr)) if callable(cf) else str(cf)) if cf is not None \
                            else s.get("max_per_wallet")
                        out.append(e)
                    return self._send(200, {"stages": out})
                if p[:4] == ["api", "v2", "chain", "robinhood"] and len(p) == 6 and p[4] == "contract":
                    slug = outer.contracts.get(p[5].lower())
                    return self._send(200, {"collection": slug}) if slug else self._send(404, {"errors": ["nf"]})
                return self._send(404, {"errors": ["no route"]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path))
                if self.path.startswith("/api/v2/auth/"):
                    return outer.auth(self, body)
                if not self._auth():
                    return self._send(401, {"errors": ["Invalid API key"]})
                p = self.path.strip("/").split("/")
                if p[:3] == ["api", "v2", "drops"] and len(p) == 5 and p[4] == "mint":
                    f = outer.mint.get(p[3])
                    if not f:
                        return self._send(404, {"errors": ["not found"]})
                    outer.mint_bodies.append(dict(body, _key=self.headers.get("X-API-KEY"), _slug=p[3]))
                    import inspect
                    nargs = len(inspect.signature(f).parameters)
                    code, b = f(body.get("minter"), body.get("quantity")) if nargs >= 2 else f(body.get("minter"))
                    return self._send(code, b)
                return self._send(404, {"errors": ["no route"]})

            def do_DELETE(self):
                outer.calls.append(("DELETE", self.path))
                outer.pats.pop(self.path.rsplit("/", 1)[-1], None)
                return self._send(200, {})

        self.H = H
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def auth(self, h, body):
        """Real SIWE semantics: rebuild the message from fields, recover the signer, compare to address."""
        import secrets
        from eth_account import Account
        from eth_account.messages import encode_defunct
        path = h.path
        if path == "/api/v2/auth/siwe/nonce":
            n = secrets.token_hex(8); self.nonces.add(n); return h._send(200, {"nonce": n})
        if path == "/api/v2/auth/siwe/verify":
            m = body["message"]
            if m["nonce"] not in self.nonces:
                return h._send(400, {"error": "bad nonce"})
            self.nonces.discard(m["nonce"])
            text = (f"{m['domain']} wants you to sign in with your {m['accountType']} account:\n{m['address']}\n\n{m['statement']}\n\n"
                    f"URI: {m['uri']}\nVersion: {m['version']}\nChain ID: {m['chainId']}\nNonce: {m['nonce']}\nIssued At: {m['issuedAt']}")
            rec = Account.recover_message(encode_defunct(text=text), signature=body["signature"])
            if rec.lower() != m["address"].lower():
                return h._send(400, {"error": "Invalid signature"})
            sid = secrets.token_hex(8); self.sessions[sid] = rec.lower()
            h.send_response(200); b = b"{}"
            h.send_header("Set-Cookie", f"access_token={sid}; Path=/"); h.send_header("Content-Length", "2"); h.end_headers(); h.wfile.write(b)
            return
        if path == "/api/v2/auth/tokens":
            sid = (h.headers.get("Cookie") or "").split("access_token=")[-1].split(";")[0]
            addr = self.sessions.get(sid)
            if not addr:
                return h._send(401, {"error": "no session"})
            tid, tok = secrets.token_hex(4), secrets.token_hex(48)
            self.pats[tid] = (tok, addr)
            return h._send(200, {"id": tid, "token": tok, "scopes": body.get("scopes")})
        if path == "/api/v2/auth/tokens/exchange":
            for tid, (tok, addr) in self.pats.items():
                if tok == body.get("subjectToken"):
                    j = secrets.token_hex(32); self.jwts[j] = addr
                    return h._send(200, {"accessToken": j, "tokenType": "Bearer", "expiresIn": 3599})
            return h._send(403, {"error": "Token exchange is not available"})
        return h._send(404, {})

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
