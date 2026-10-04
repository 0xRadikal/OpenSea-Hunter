#!/usr/bin/env python3
"""Browser-wallet signer: no private key is loaded by this process."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import aerent_mint as bot
from eth_utils import is_address, to_checksum_address

CHAIN_ID = bot.CHAIN_ID
LOCAL_HOST = "127.0.0.1"


def validate_wallet(value: str) -> str:
    if not is_address(value):
        raise SystemExit(f"invalid wallet address: {value!r}")
    return to_checksum_address(value)


def build_tx(wallet: str, calldata: str) -> dict[str, str]:
    return {"from": wallet, "to": to_checksum_address(bot.SD), "value": "0x0", "data": calldata}


class CallbackState:
    def __init__(self, token: str) -> None:
        self.token = token
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.tx_hash = None
        self.error = None

    def submit(self, token: str, tx_hash: str | None = None, error: str | None = None) -> bool:
        if not secrets.compare_digest(token, self.token):
            return False
        with self.lock:
            if self.event.is_set():
                return False
            if tx_hash:
                self.tx_hash = tx_hash
            else:
                self.error = error or "wallet returned no transaction hash"
            self.event.set()
            return True

    def wait(self, timeout: float):
        self.event.wait(timeout)
        with self.lock:
            return self.tx_hash, self.error


class Handler(BaseHTTPRequestHandler):
    state: CallbackState
    tx: dict[str, str]

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not secrets.compare_digest(token, self.state.token):
            self._send(403, "text/plain; charset=utf-8", b"invalid session")
            return

        tx_json = json.dumps(self.tx, separators=(",", ":"))
        page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenSea Hunter — Wallet Signer</title></head>
<body>
<h2>OpenSea Hunter</h2>
<p><b>No private key is requested by this page.</b></p>
<p>Expected chain: 4663 (Robinhood Chain)</p>
<p>Target: <code>{self.tx["to"]}</code></p>
<p>Value: <code>0</code></p>
<button id="go">Connect wallet and send mint</button>
<pre id="status">Waiting for action…</pre>
<script>
const tx = {tx_json};
const token = {json.dumps(self.state.token)};
const out = document.getElementById('status');
const show = x => out.textContent = x;
async function callback(payload) {{
  const r = await fetch('/callback', {{
    method:'POST',
    headers:{{'content-type':'application/json'}},
    body:JSON.stringify({{token,...payload}})
  }});
  if (!r.ok) throw new Error('local callback failed');
}}
async function run() {{
  if (!window.ethereum) throw new Error('No EIP-1193 browser wallet detected.');
  const accounts = await window.ethereum.request({{method:'eth_requestAccounts'}});
  const from = accounts && accounts[0];
  if (!from) throw new Error('No wallet account returned.');
  if (from.toLowerCase() !== tx.from.toLowerCase()) throw new Error('Connected wallet does not match the requested minter.');
  const chainId = await window.ethereum.request({{method:'eth_chainId'}});
  if (chainId.toLowerCase() !== '0x1237') throw new Error('Wrong network; switch to Robinhood Chain (4663).');
  show('Waiting for wallet approval…');
  const hash = await window.ethereum.request({{method:'eth_sendTransaction',params:[tx]}});
  if (!hash) throw new Error('Wallet returned no tx hash.');
  await callback({{tx_hash:hash}});
  show('Submitted: ' + hash);
}}
document.getElementById('go').onclick = () => run().catch(async e => {{
  show('Error: ' + (e?.message || e));
  try {{ await callback({{error:String(e?.message || e)}}); }} catch (_) {{}}
}});
</script></body></html>""".encode("utf-8")
        self._send(200, "text/html; charset=utf-8", page)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/callback":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, "text/plain; charset=utf-8", b"invalid json")
            return
        tx_hash = payload.get("tx_hash")
        error = payload.get("error")
        if tx_hash is not None and (not isinstance(tx_hash, str) or not tx_hash.startswith("0x")):
            tx_hash, error = None, "invalid transaction hash"
        if not self.state.submit(str(payload.get("token", "")), tx_hash, str(error) if error else None):
            self._send(403, "text/plain; charset=utf-8", b"invalid or expired callback")
            return
        self._send(200, "text/plain; charset=utf-8", b"ok")

    def log_message(self, fmt: str, *args: object) -> None:
        print("[browser-signer]", fmt % args, flush=True)


def start_server(tx: dict[str, str], state: CallbackState, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {})
    handler.tx, handler.state = tx, state
    return ThreadingHTTPServer((LOCAL_HOST, port), handler)


def wait_receipt(tx_hash: str, timeout: float = 45.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for rpc_url in bot.READ_RPCS:
            try:
                result = bot.rpc("eth_getTransactionReceipt", [tx_hash], url=rpc_url, timeout=4)
                if result.get("result"):
                    return result["result"]
            except Exception:
                pass
        time.sleep(0.20)
    return None


def fetch_valid_calldata(wallet: str, api_key: str, timeout_s: float = 25.0) -> dict:
    deadline = time.time() + timeout_s
    session = bot.new_session()
    while time.time() < deadline:
        try:
            code, body, _ = bot.api_mint(session, api_key, wallet)
        except Exception:
            time.sleep(0.5)
            continue
        if code == 200:
            ok, reason = bot.validate(body, wallet)
            if not ok:
                raise SystemExit("OpenSea calldata failed safety validation: " + reason)
            return body
        if code in (401, 403):
            raise SystemExit(f"OpenSea API authentication rejected: HTTP {code}")
        time.sleep(1.2)
    raise SystemExit("timed out waiting for a valid OpenSea mint response")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint through browser wallet without private key in Python")
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--opensea-api-key", default=None)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--prepare-only", action="store_true")
    args = ap.parse_args()

    wallet = validate_wallet(args.wallet)
    keys = [x.strip() for x in (args.opensea_api_key or "").split(",") if x.strip()]
    if not keys:
        keys = [x.strip() for x in os.environ.get("OPENSEA_API_KEYS", "").split(",") if x.strip()]
    if not keys:
        raise SystemExit("missing OpenSea API key")

    chain_id = int(bot.rpc("eth_chainId", [])["result"], 16)
    if chain_id != CHAIN_ID:
        raise SystemExit(f"wrong RPC network: expected {CHAIN_ID}, got {chain_id}")
    if bot.rpc("eth_getCode", [wallet, "latest"])["result"] not in ("0x", "0x0"):
        raise SystemExit("browser signer mode expects an EOA wallet address")
    if bot.nft_balance(wallet) >= bot.TARGET_HELD:
        print("wallet already holds the NFT; nothing to do")
        return 0

    start, _end, stage, _drop = bot.api_stage_times(keys[0])
    if stage.get("price") != "0":
        raise SystemExit(f"refusing non-free stage: price={stage.get('price')!r}")
    print(f"stage={stage.get('label')} price={stage.get('price')}")
    if time.time() < start:
        bot.precise_sleep_until(start)

    body = fetch_valid_calldata(wallet, keys[0])
    signed_start, signed_end = bot.signed_window(body["data"])
    if time.time() > signed_end:
        raise SystemExit("signed mint window already closed")
    if time.time() < signed_start:
        bot.precise_sleep_until(signed_start + bot.FIRE_GUARD_S)

    tx = build_tx(wallet, body["data"])
    if args.prepare_only:
        print("PREPARE-ONLY: no wallet interaction and no broadcast")
        return 0

    token = secrets.token_urlsafe(32)
    state = CallbackState(token)
    server = start_server(tx, state, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Open this URL in a browser on the same machine:\nhttp://{LOCAL_HOST}:{server.server_port}/?token={token}")
    print("The wallet must be the same address and must be on Robinhood Chain.")

    try:
        tx_hash, error = state.wait(max(5.0, signed_end - time.time()))
        if error:
            raise SystemExit("wallet did not submit: " + error)
        if not tx_hash:
            raise SystemExit("wallet callback timed out")
        receipt = wait_receipt(tx_hash)
        if not receipt:
            raise SystemExit("transaction submitted but no receipt arrived before timeout")
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise SystemExit("transaction mined but reverted: " + tx_hash)
        print("MINTED:", tx_hash)
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
