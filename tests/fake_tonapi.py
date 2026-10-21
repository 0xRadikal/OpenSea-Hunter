"""Fake TonAPI v2 (tests only). Serves /v2/blockchain/accounts/{id}/transactions with the same response shape,
ordering and after_lt semantics as the real API (verified live 2026-10-21: after_lt is exclusive, sort_order=asc|desc),
plus /v2/rates. Transactions are built in exactly the structure TonAPI returns (see tests/fixture_tonapi_txs.json)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeTonApi:
    def __init__(self, account_raw, key="testkey"):
        self.account, self.key = account_raw, key
        self.txs = []
        self.lt = 1_000_000
        self.fail = 0            # next N requests answer 500 (outage simulation)
        self.calls = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                code, body = outer.handle(u.path, q, self.headers.get("Authorization", ""))
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def handle(self, path, q, auth):
        with self.lock:
            self.calls.append((path, q))
            if auth != f"Bearer {self.key}":
                return 401, {"error": "unauthorized"}
            if self.fail > 0:
                self.fail -= 1
                return 500, {"error": "internal"}
            if path == "/v2/rates":
                return 200, {"rates": {"TON": {"prices": {"USD": 2.0}}}}
            pre = "/v2/blockchain/accounts/"
            if path.startswith(pre) and path.endswith("/transactions"):
                if path[len(pre):-len("/transactions")] != self.account:
                    return 200, {"transactions": []}
                lim = int(q.get("limit", 100))
                txs = sorted(self.txs, key=lambda t: t["lt"], reverse=q.get("sort_order", "desc") == "desc")
                if "after_lt" in q:
                    txs = [t for t in txs if t["lt"] > int(q["after_lt"])]
                return 200, {"transactions": txs[:lim]}
            return 404, {"error": "not found"}

    def pay(self, value_nano, comment=None, *, success=True, bounced=False, dest=None, utime=None, src="0:" + "ab" * 32,
            msg_type="int_msg", tx_type="TransOrd", aborted=False):
        """Append a transaction in TonAPI's shape. Returns its hash."""
        import time
        with self.lock:
            self.lt += 1000
            h = f"{self.lt:064x}"
            im = {"msg_type": msg_type, "created_lt": self.lt - 1, "ihr_disabled": True, "bounce": False, "bounced": bounced,
                  "value": int(value_nano), "fwd_fee": 0, "ihr_fee": 0, "import_fee": 0, "created_at": int(utime or time.time()),
                  "hash": f"{self.lt + 7:064x}", "source": {"address": src, "is_scam": False, "is_wallet": True},
                  "destination": {"address": dest or self.account, "is_scam": False, "is_wallet": True}}
            if comment is not None:
                im.update({"op_code": "0x00000000", "decoded_op_name": "text_comment", "decoded_body": {"text": comment}})
            self.txs.append({"hash": h, "lt": self.lt, "account": {"address": self.account, "is_scam": False, "is_wallet": True},
                             "success": success, "utime": int(utime or time.time()), "orig_status": "active",
                             "end_status": "active", "total_fees": 1, "end_balance": 1, "transaction_type": tx_type,
                             "state_update_old": "", "state_update_new": "", "in_msg": im, "out_msgs": [],
                             "block": "(0,1,1)", "aborted": aborted, "destroyed": False})
            return h

    def close(self):
        self.srv.shutdown(); self.srv.server_close()
