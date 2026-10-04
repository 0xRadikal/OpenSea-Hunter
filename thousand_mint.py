#!/usr/bin/env python3
"""
Thousand (thousandart) WL mint bot — Robinhood Chain 4663, ERC721SeaDrop clone.

Verified facts (2026-09-29 ~16:05 UTC):
  NFT      0x0b10721af018aadfbb3e31d0f4d2dea2d6bd994b  (EIP-1167 -> ERC721SeaDropCloneable)
  SeaDrop  0x00005EA00Ac477B1030CE78506496e8C2dE24bf5
  WL stage = signed_presale (mintSigned). Signature is issued ONLY by OpenSea API
            POST /api/v2/drops/thousandart/mint, and only while the stage is ACTIVE.
  Stages: TEAM ends / WL starts 16:30:00Z, WL ends 17:30:00Z, PUBLIC 17:30-18:30Z.
  API mint endpoint resource limit: 5 req / ~60 s  -> no spamming, paced retries.

Flow: sleep -> warm connections -> at T0 ask API for calldata -> strict validation
      (to == SeaDrop, value == 0, selector mintSigned/mintPublic, nft == Thousand,
      quantity == 1, recipient == me) -> sign -> broadcast to all RPCs in parallel
      -> watch receipt -> on revert decode reason and retry (bounded gas budget).
"""
import argparse, json, os, re, sys, time, threading
import datetime as dt
import requests
from eth_account import Account
from eth_utils import keccak, to_checksum_address

SLUG = "thousandart"
NFT = "0x0b10721af018aadfbb3e31d0f4d2dea2d6bd994b"
SD = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
CHAIN_ID = 4663
RPCS = ["https://robinhood.drpc.org", "https://rpc.mainnet.chain.robinhood.com"]
API = f"https://api.opensea.io/api/v2/drops/{SLUG}/mint"
WL_START = 1790699400               # 2026-09-29 16:30:00Z (verified == API WL start_time)
PUBLIC_START = 1790703000
PUBLIC_END = 1790706600
SEL_SIGNED = "0x" + keccak(text="mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)")[:4].hex()
SEL_PUBLIC = "0x" + keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()
GAS_LIMIT = 400_000
MAX_FEE = 500_000_000          # 0.5 gwei cap (baseFee measured ~0.021 gwei); reserve 0.0002 ETH
PRIO = 0                        # Arbitrum FIFO sequencer: tip buys nothing
MAX_BROADCASTS = 6              # hard cap on txs that can cost gas
GWEI = 10**9

_SECRETS = []
_lock = threading.Lock()


def mask(t):
    t = str(t)
    for s in _SECRETS:
        if s:
            t = t.replace(s, "<KEY>")
    return t


def log(m):
    with _lock:
        print(f"[{dt.datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]}] {mask(m)}", flush=True)


def load_key(path):
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        v = (line.split("=", 1)[1] if "=" in line else line).strip().strip('"').strip("'")
        if re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", v):
            v = v if v.startswith("0x") else "0x" + v
            _SECRETS.extend([v, v[2:]])
            return v
    raise SystemExit(f"no valid key in {path}")


def load_api_keys():
    ks = [k.strip() for k in os.environ.get("OPENSEA_API_KEYS", "").split(",") if k.strip()]
    kf = os.environ.get("OPENSEA_KEYS_FILE", "/root/opensea-keys.env")
    if not ks and os.path.isfile(kf):
        ks = [k.strip() for k in open(kf).read().split("=", 1)[1].split(",") if k.strip()]
    if not ks:
        raise SystemExit("no OpenSea API keys")
    ks = list(dict.fromkeys(ks))          # dedupe
    _SECRETS.extend(ks)
    return ks


S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})


def rpc(method, params, url=None, timeout=6):
    urls = [url] if url else RPCS
    last = None
    for u in urls:
        try:
            r = S.post(u, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=timeout)
            return r.json()
        except Exception as e:
            last = e
    raise RuntimeError(f"RPC failed: {last}")


def word(data, i):
    return data[10 + 64 * i: 10 + 64 * (i + 1)]


def validate(tx, me):
    """Return (ok, reason). Refuse anything that is not exactly a free 1x mint of Thousand to `me`."""
    to = (tx.get("to") or "").lower()
    data = (tx.get("data") or "").lower()
    val = tx.get("value", "0x0")
    try:
        v = int(val, 16) if isinstance(val, str) and val.startswith("0x") else int(val)
    except Exception:
        return False, f"bad value {val!r}"
    if to != SD:
        return False, f"target {to} is not SeaDrop"
    if v != 0:
        return False, f"value {v} != 0 (drop is free)"
    if not re.fullmatch(r"0x[0-9a-f]+", data) or len(data) < 10 + 64 * 4:
        return False, "calldata malformed"
    sel = data[:10]
    if sel not in (SEL_SIGNED, SEL_PUBLIC):
        return False, f"unexpected selector {sel}"
    if int(word(data, 0), 16) != int(NFT, 16):
        return False, "nft arg mismatch"
    minter_if_not_payer = int(word(data, 2), 16)
    if minter_if_not_payer not in (0, int(me, 16)):
        return False, "recipient is not this wallet"
    if int(word(data, 3), 16) != 1:
        return False, f"quantity {int(word(data,3),16)} != 1"
    if sel == SEL_SIGNED:
        price = int(word(data, 4), 16)      # mintParams.mintPrice
        if price != 0:
            return False, f"signed mintPrice {price} != 0"
    return True, "mintSigned" if sel == SEL_SIGNED else "mintPublic"


def api_mint(apikey, me):
    r = S.post(API, headers={"X-API-KEY": apikey}, json={"minter": me, "quantity": 1}, timeout=8)
    rem = r.headers.get("x-resource-ratelimit-remaining")
    rst = r.headers.get("x-resource-ratelimit-reset")
    ra = r.headers.get("retry-after")
    if r.status_code == 429 and ra and not rst:
        rst = str(int(time.time() + float(ra)))
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    return r.status_code, body, (int(rem) if rem else None), (int(rst) if rst else None)


def nft_balance(me):
    r = rpc("eth_call", [{"to": NFT, "data": "0x70a08231" + me[2:].lower().rjust(64, "0")}, "latest"])
    return int(r.get("result", "0x0"), 16)


def sleep_until(ts, label):
    while True:
        rem = ts - time.time()
        if rem <= 0:
            return
        if rem > 120:
            log(f"{label}: T-{rem/60:.1f} min")
            time.sleep(min(60, rem - 60))
        elif rem > 3:
            time.sleep(min(1, rem - 2.5))
        else:
            time.sleep(max(0, rem - 0.004)) if rem > 0.02 else None


def broadcast(raw):
    res = {}
    def f(u):
        try:
            res[u] = rpc("eth_sendRawTransaction", [raw], u, timeout=5)
        except Exception as e:
            res[u] = {"error": {"message": str(e)[:80]}}
    ts = [threading.Thread(target=f, args=(u,)) for u in RPCS]
    [t.start() for t in ts]
    [t.join(6) for t in ts]
    return res


def wait_receipt(h, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        for u in RPCS:
            try:
                rc = rpc("eth_getTransactionReceipt", [h], u, timeout=3).get("result")
                if rc:
                    return rc
            except Exception:
                pass
        time.sleep(0.1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", default=os.environ.get("MINT_KEY_FILE", "/root/mint-wallet.env"))
    ap.add_argument("--arm", action="store_true", help="really broadcast")
    ap.add_argument("--start", type=float, default=WL_START, help="unix time to start asking API")
    ap.add_argument("--no-public-fallback", action="store_true")
    ap.add_argument("--stage-label", default="WL")
    a = ap.parse_args()

    acct = Account.from_key(load_key(a.key_file))
    me = acct.address
    keys = load_api_keys()
    apikey = keys[0]
    kidx = 0
    blocked = {}
    log(f"THOUSAND WL BOT  wallet={me[:6]}...{me[-4:]}  mode={'ARMED' if a.arm else 'DRY-RUN'}")

    assert int(rpc("eth_chainId", [])["result"], 16) == CHAIN_ID, "wrong chain"
    assert rpc("eth_getCode", [me, "latest"])["result"] in ("0x", "0x0"), "wallet is a contract"
    bal = int(rpc("eth_getBalance", [me, "latest"])["result"], 16)
    held = nft_balance(me)
    log(f"balance {bal/1e18:.8f} ETH | holds {held} Thousand | reserve needed {GAS_LIMIT*MAX_FEE/1e18:.6f} ETH")
    if held >= 1:
        log("already holds a Thousand — nothing to do"); return
    if bal < GAS_LIMIT * MAX_FEE:
        raise SystemExit("balance too low for gas reserve")

    sleep_until(a.start - 20, "WL")
    # warm TLS connections + fresh nonce just before T0
    nonce = int(rpc("eth_getTransactionCount", [me, "pending"])["result"], 16)
    for u in RPCS:
        try: rpc("eth_blockNumber", [], u)
        except Exception: pass
    try: S.get(f"https://api.opensea.io/api/v2/drops/{SLUG}", headers={"X-API-KEY": apikey}, timeout=5)
    except Exception: pass
    log(f"warmed up, nonce={nonce}")
    sleep_until(a.start - 0.3, "WL")
    # Poll the READ endpoint (separate 600/h bucket) until the API itself says WL is active,
    # so the scarce mint-endpoint budget (5/min) is spent only when it can succeed.
    t_poll = time.time()
    while time.time() - t_poll < 25:
        try:
            g = S.get(f"https://api.opensea.io/api/v2/drops/{SLUG}", headers={"X-API-KEY": apikey}, timeout=4).json()
            lab = (g.get("active_stage") or {}).get("label")
        except Exception as e:
            lab = f"err {str(e)[:40]}"
        if lab == a.stage_label:
            log(f"API active_stage = {lab}"); break
        time.sleep(0.2)
    else:
        log("active_stage did not flip within 25s — trying mint endpoint anyway")
    log(">>> requesting signed mint from OpenSea API")

    broadcasts = 0
    tx_data = None
    deadline = PUBLIC_END if not a.no_public_fallback else PUBLIC_START
    backoff = [0.35, 0.6, 1.0, 1.5]
    attempt = 0
    while time.time() < deadline and broadcasts < MAX_BROADCASTS:
        if tx_data is None:
            t0 = time.time()
            try:
                code, body, rem, rst = api_mint(apikey, me)
            except Exception as e:
                log(f"API error {str(e)[:80]}"); time.sleep(0.5); continue
            log(f"API {code} in {(time.time()-t0)*1000:.0f} ms (resource remaining={rem})")
            if code == 200:
                ok, why = validate(body, me)
                if not ok:
                    log(f"REFUSED calldata: {why}"); raise SystemExit(2)
                tx_data = body
                log(f"calldata OK ({why})")
            else:
                msg = json.dumps(body)[:300]
                log(f"API says: {msg}")
                if code == 422 and "not on this stage's allowlist" in msg and "'WL'" in msg:
                    log("!! wallet NOT on WL allowlist. Waiting for PUBLIC stage (17:30Z).")
                    if a.no_public_fallback: return
                    if time.time() < PUBLIC_START:
                        sleep_until(PUBLIC_START, "PUBLIC")
                    time.sleep(1.5); continue
                if code == 422 and ("limit" in msg.lower() and "wallet" in msg.lower()):
                    if nft_balance(me) >= 1:
                        log("*** wallet already holds the NFT ***"); return
                # mint endpoint budget is ~5 calls per key (observed refill within ~1 min, header says ~1 h).
                # Pace: 1.2 s between attempts; if exhausted or 429, wait 15 s and try again.
                # measured: 5 mint calls per key per ~5 min, buckets are per key.
                if code in (401, 403) or code == 429 or (rem is not None and rem <= 0):
                    blocked[kidx] = (rst or time.time() + 300) if code != 401 and code != 403 else time.time() + 10**6
                    free = [i for i in range(len(keys)) if blocked.get(i, 0) <= time.time()]
                    if free:
                        kidx = free[0]; apikey = keys[kidx]
                        log(f"key budget exhausted/rejected -> switching to key #{kidx}"); time.sleep(1.0)
                    else:
                        # token bucket refills gradually (observed) -> retry after <=12 s, never sit for minutes
                        w = min(12, max(1, min(blocked.values()) - time.time() + 0.5))
                        log(f"all keys exhausted -> waiting {w:.0f}s"); time.sleep(w)
                        kidx = min(blocked, key=blocked.get); apikey = keys[kidx]
                else:
                    time.sleep(2.0)
                attempt += 1
                continue

        # never broadcast before the signed startTime: a tx mined in an earlier-timestamp block reverts (costs gas)
        if tx_data["data"].lower()[:10] == SEL_SIGNED:
            st = int(word(tx_data["data"].lower(), 6), 16)
            en = int(word(tx_data["data"].lower(), 7), 16)
            if broadcasts == 0:
                log(f"signed window {st} -> {en}  (now {time.time():.2f})")
            if time.time() > en:
                log("signed window already over"); tx_data = None; continue
            # block timestamps measured ~1 s behind wall clock: fire only once the LATEST block ts >= startTime
            while True:
                try:
                    bts = int(rpc("eth_getBlockByNumber", ["latest", False], RPCS[0], timeout=2)["result"]["timestamp"], 16)
                except Exception:
                    bts = int(time.time()) - 2
                if bts >= st:
                    break
                time.sleep(0.02)
        if not a.arm:
            sim = rpc("eth_call", [{"from": me, "to": SD, "data": tx_data["data"], "value": "0x0"}, "latest"])
            log(f"DRY-RUN: simulation -> {'OK' if 'result' in sim else sim.get('error')}")
            log("DRY-RUN: not broadcasting."); return

        signed = acct.sign_transaction({"to": to_checksum_address(SD), "value": 0, "data": tx_data["data"],
                                        "nonce": nonce, "chainId": CHAIN_ID, "gas": GAS_LIMIT, "type": 2,
                                        "maxFeePerGas": MAX_FEE, "maxPriorityFeePerGas": PRIO})
        raw = signed.raw_transaction.to_0x_hex()
        h = "0x" + signed.hash.hex().removeprefix("0x")
        res = broadcast(raw)
        broadcasts += 1
        log(f"broadcast #{broadcasts} nonce={nonce} tx={h}  " +
            " | ".join(f"{u[8:22]}:{'ok' if 'result' in r else r.get('error',{}).get('message','?')[:50]}" for u, r in res.items()))
        if not any("result" in r or "known" in json.dumps(r) for r in res.values()):
            # rejected by every node (e.g. nonce) — refresh nonce and retry
            nonce = int(rpc("eth_getTransactionCount", [me, "pending"])["result"], 16)
            time.sleep(0.2); continue
        rc = wait_receipt(h)
        if rc and int(rc["status"], 16) == 1:
            log(f"*** MINTED *** block {int(rc['blockNumber'],16)} gasUsed {int(rc['gasUsed'],16)}; holds {nft_balance(me)}")
            return
        nonce += 1 if rc else 0
        sim = rpc("eth_call", [{"from": me, "to": SD, "data": tx_data["data"], "value": "0x0"}, "latest"])
        log(f"tx {'REVERTED' if rc else 'no receipt'}; current simulation: {sim.get('error', 'OK')}")
        if nft_balance(me) >= 1:
            log("*** wallet holds the NFT ***"); return
        err = json.dumps(sim.get("error", ""))
        if "result" in sim:
            continue                      # timing revert, now passes -> resend same calldata
        tx_data = None                    # otherwise ask API for fresh calldata
        time.sleep(0.4)
    log("stopped: deadline or broadcast cap reached. holds=%d" % nft_balance(me))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
