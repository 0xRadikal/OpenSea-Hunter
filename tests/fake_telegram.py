"""Fake Telegram Bot API server (tests only). Records every bot call; tests inject updates via getUpdates."""
import cgi
import html as _html
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeTelegram:
    def __init__(self):
        self.updates, self.calls = [], []
        self.uid = 1
        self.mid = 1000
        self.cv = threading.Condition()
        self.reject_rich = False          # simulate a Bot API server without Rich Messages (tests the classic fallback)
        self.bot = {"id": 999, "is_bot": True, "first_name": "osh", "username": "osh_bot"}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n)
                ct = self.headers.get("Content-Type", "")
                params = {}
                if "multipart" in ct:
                    fs = cgi.FieldStorage(fp=io.BytesIO(raw), headers=self.headers, environ={"REQUEST_METHOD": "POST"})
                    params = {k: fs.getvalue(k) for k in fs.keys()}
                elif "json" in ct:
                    params = json.loads(raw or b"{}")
                else:
                    params = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                for k, v in list(params.items()):
                    if isinstance(v, str) and v[:1] in "[{":
                        try:
                            params[k] = json.loads(v)
                        except ValueError:
                            pass
                method = self.path.rsplit("/", 1)[-1]
                res = outer.handle(method, params)
                if isinstance(res, tuple):                                  # (http_status, error description)
                    b = json.dumps({"ok": False, "error_code": res[0], "description": res[1]}).encode()
                    self.send_response(res[0])
                else:
                    b = json.dumps({"ok": True, "result": res}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def handle(self, method, p):
        if method == "getMe":
            return self.bot
        if method in ("deleteWebhook", "setMyCommands", "answerCallbackQuery", "answerPreCheckoutQuery", "deleteMessage"):
            with self.cv:
                self.calls.append((method, p)); self.cv.notify_all()
            return True
        if method == "getUpdates":
            off = int(p.get("offset", 0) or 0)
            end = time.time() + min(float(p.get("timeout", 0) or 0), 1.0)
            with self.cv:
                while True:
                    ups = [u for u in self.updates if u["update_id"] >= off]
                    if ups or time.time() >= end:
                        return ups
                    self.cv.wait(0.05)
        if isinstance(p.get("rich_message"), dict):
            if self.reject_rich:
                with self.cv:
                    self.calls.append((method + ":rejected", p)); self.cv.notify_all()
                return (404, "Not Found") if method == "sendRichMessage" else (400, "Bad Request: rich messages unsupported")
            for m in p["rich_message"].get("media") or []:          # like Telegram: attach:// must be uploaded
                ref = str(m.get("media", {}).get("media", ""))
                if ref.startswith("attach://") and not isinstance(p.get(ref[9:]), (bytes, bytearray)):
                    return (400, f"Bad Request: file {ref} not found")
                if f'tg://photo?id={m.get("id")}' not in p["rich_message"].get("html", ""):
                    return (400, "Bad Request: media is not used")
            p["_rich"] = p["rich_message"]
            p["text"] = rich_text(p["rich_message"].get("html", ""))
        elif isinstance(p.get("text"), str) and "<tg-emoji" in p["text"]:
            # keep the raw HTML (premium custom emoji) and expose a plain view for text assertions
            p["_raw_text"] = p["text"]
            p["text"] = re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", p["text"], flags=re.S)
        with self.cv:
            self.calls.append((method, p)); self.cv.notify_all()
            self.mid += 1
            mid = self.mid
        if method in ("sendMessage", "sendInvoice", "editMessageText", "sendRichMessage"):
            return {"message_id": int(p.get("message_id") or mid), "date": int(time.time()),
                    "chat": {"id": int(p.get("chat_id", 0)), "type": "private"}, "text": p.get("text", "")}
        return True

    # ---------------------------------------------------------------- update injection
    def _push(self, upd):
        with self.cv:
            upd["update_id"] = self.uid; self.uid += 1
            self.updates.append(upd); self.cv.notify_all()

    def user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"u{uid}", "username": f"u{uid}"}

    def text(self, uid, text):
        with self.cv:
            self.mid += 1; mid = self.mid
        msg = {"message_id": mid, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
               "from": self.user(uid), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self._push({"message": msg})
        return mid

    def callback(self, uid, data, msg_id=1):
        self._push({"callback_query": {"id": str(time.time()), "from": self.user(uid), "chat_instance": "x", "data": data,
                                       "message": {"message_id": msg_id, "date": int(time.time()),
                                                   "chat": {"id": uid, "type": "private"}, "text": "."}}})

    def webapp(self, uid, data):
        with self.cv:
            self.mid += 1; mid = self.mid
        self._push({"message": {"message_id": mid, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                                "from": self.user(uid), "web_app_data": {"data": data, "button_text": "k"}}})

    def precheckout(self, uid, payload, amount):
        self._push({"pre_checkout_query": {"id": "pc" + str(self.uid), "from": self.user(uid), "currency": "XTR",
                                           "total_amount": amount, "invoice_payload": payload}})

    def paid(self, uid, payload, amount, charge):
        with self.cv:
            self.mid += 1; mid = self.mid
        self._push({"message": {"message_id": mid, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                                "from": self.user(uid), "successful_payment": {
                                    "currency": "XTR", "total_amount": amount, "invoice_payload": payload,
                                    "telegram_payment_charge_id": charge, "provider_payment_charge_id": ""}}})

    # ---------------------------------------------------------------- assertions helpers
    def wait(self, pred, timeout=20, since=0):
        end = time.time() + timeout
        with self.cv:
            while time.time() < end:
                for c in self.calls[since:]:
                    if pred(c):
                        return c
                self.cv.wait(0.05)
        raise AssertionError("timeout; last calls: " + json.dumps([(m, str(p)[:160]) for m, p in self.calls[-8:]], ensure_ascii=False))

    def wait_text(self, uid, needle, timeout=20, since=0):
        c = self._wait_text(uid, needle, timeout, since)
        if os.environ.get("FAKE_TG_DUMP"):
            print(f"\n@@ {needle!r} [{c[0]}]\n{c[1].get('text', '')}\n@@end", flush=True)
        return c

    def _wait_text(self, uid, needle, timeout=20, since=0):
        return self.wait(lambda c: c[0] in ("sendMessage", "editMessageText", "sendRichMessage") and str(c[1].get("chat_id")) == str(uid)
                         and needle in c[1].get("text", ""), timeout, since)

    def mark(self):
        with self.cv:
            return len(self.calls)

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


def rich_text(h):
    """Readable plain-text view of a rich-HTML message (for assertions): block tags -> newlines, table cells -> ' | ',
    checkboxes -> [x]/[ ], custom emoji -> their fallback emoji, bidi isolates removed, entities unescaped."""
    h = re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", h, flags=re.S)
    h = re.sub(r'<input type="checkbox" checked>', "[x] ", h)
    h = re.sub(r'<input type="checkbox">', "[ ] ", h)
    h = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", h)
    h = re.sub(r"<br\s*/?>|</?(p|li|tr|h[1-6]|ul|ol|table|caption|aside|footer|details|summary|blockquote|hr|tg-math-block)[^>]*>",
               "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h).replace("\u2066", "").replace("\u2069", "").replace("\u200f", "")
    return re.sub(r"\n\s*\n+", "\n", h).strip()
