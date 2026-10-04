#!/usr/bin/env python3
"""
AERENT OG Pass — OG List mint bot (v2).  Robinhood Chain 4663, ERC721SeaDrop clone.

Verified 2026-10-03 (on-chain + OpenSea API):
  NFT       0xa655bbe47a8dec02991452adc7bef76bbbeceee9 (EIP-1167 -> same impl as Thousand)
  SeaDrop   0x00005EA00Ac477B1030CE78506496e8C2dE24bf5, signer 0xfce4...f8c3 (OpenSea)
  OG List   signed_presale, 16:30:00Z -> 17:30:00Z, price 0, max 1/wallet, 3884 wallets, supply 1000
  Public    17:30Z, price 0.1 ETH  -> this bot NEVER mints public (value is hard-coded 0).

Speed design (each item is a measured bottleneck from the Thousand run, total 1.56 s):
  * No read-endpoint polling before asking for the signature: OpenSea flipped stages at T0
    (WL seen active at T0+0.16 s, TEAM at T0+<1 s). Mint requests are fired at T0 in a hedged,
    staggered wave on pre-warmed, per-thread connections; first valid answer wins.
  * No block-timestamp polling before broadcast (cost ~0.7 s last time): the sequencer clock was
    measured aligned with NTP (new second visible via drpc at +0.165 s), and OpenSea only signs once
    the stage is active, so we only enforce wall-clock >= mintParams.startTime + guard.
  * Broadcast to the SEQUENCER endpoint directly (107 ms RTT) + official RPC + drpc in parallel.
  * Gas limit 1.2M (last mint used 338,835 of which 206,247 was L1 component) bounded by balance.
"""
import argparse, json, os, re, sys, time, threading
import datetime as dt
import requests
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from eth_abi import encode as abi_encode, decode as abi_decode

# ---------------------------------------------------------------- config (verified values)
SLUG = "aerent"
NFT = "0xa655bbe47a8dec02991452adc7bef76bbbeceee9"
SD = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
CHAIN_ID = 4663
STAGE_LABEL = "OG List"
STAGE_START = 1791045000            # 2026-10-03 16:30:00Z  (== API start_time)
STAGE_END = 1791048600              # 2026-10-03 17:30:00Z  (== API end_time)
SEND_RPCS = ["https://sequencer.mainnet.chain.robinhood.com",
             "https://rpc.mainnet.chain.robinhood.com",
             "https://robinhood.drpc.org"]
READ_RPCS = ["https://robinhood.drpc.org", "https://rpc.mainnet.chain.robinhood.com"]
API_BASE = "https://api.opensea.io/api/v2/drops"
GAS_LIMIT = 1_200_000
MAX_FEE_CAP = 400_000_000           # 0.4 gwei cap (baseFee ~0.025 gwei measured)
TARGET_HELD = 1                     # stop once the wallet holds this many (OG List limit is 1)
# Priority fee: set equal to maxFee at runtime. Measured on this chain (355 SeaDrop txs of the Thousand
# rush + 25 recent txs): effectiveGasPrice == baseFee in 100% of cases, tips up to 3 gwei were never
# charged. So a max tip costs nothing extra; it is set only so no possible tip-based ordering is missed.
PRIO_EQUALS_MAXFEE = True
MAX_BROADCASTS = 4                  # hard cap on gas-spending txs
FIRE_GUARD_S = 0.20                 # wall >= startTime + guard (sequencer clock measured <=0.165 s behind)
# hedged wave: (offset seconds after T0, key index)
WAVE = [(0.030, 0), (0.180, 0)]       # 2 mint calls at T0; key #1 is failover only (OpenSea: extra keys != throughput)

SEL_SIGNED = "0x" + keccak(text="mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)")[:4].hex()
SEL_PUBLIC = "0x" + keccak(text="mintPublic(address,address,address,uint256)")[:4].hex()

_SECRETS = []
_plock = threading.Lock()


def mask(t):
    t = str(t)
    for s in _SECRETS:
        if s:
            t = t.replace(s, "<SECRET>")
    return t


def log(m):
    with _plock:
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


def load_api_keys(path=os.environ.get("OPENSEA_KEYS_FILE", "/root/opensea-keys.env")):
    ks = [k.strip() for k in os.environ.get("OPENSEA_API_KEYS", "").split(",") if k.strip()]
    if not ks and os.path.isfile(path):
        ks = [k.strip() for k in open(path).read().split("=", 1)[1].split(",") if k.strip()]
    ks = list(dict.fromkeys(ks))
    if not ks:
        raise SystemExit("no OpenSea API keys")
    _SECRETS.extend(ks)
    return ks


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    a = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
    s.mount("https://", a)
    s.mount("http://", a)
    return s


_main_s = new_session()


def rpc(method, params, url=None, timeout=5, sess=None):
    s = sess or _main_s
    last = None
    for u in ([url] if url else READ_RPCS):
        try:
            return s.post(u, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                          timeout=timeout).json()
        except Exception as e:
            last = e
    raise RuntimeError(f"RPC failed: {last}")


def word(data, i):
    return data[10 + 64 * i: 10 + 64 * (i + 1)]


def validate(tx, me):
    """Accept ONLY: SeaDrop.mintSigned/mintPublic for this NFT, qty 1, value 0, price 0, to me."""
    if not isinstance(tx, dict):
        return False, "not a dict"
    to = str(tx.get("to") or "").lower()
    data = str(tx.get("data") or "").lower()
    val = tx.get("value", "0x0")
    try:
        v = int(val, 16) if isinstance(val, str) and val.lower().startswith("0x") else int(val)
    except Exception:
        return False, f"bad value {val!r}"
    if to != SD:
        return False, f"target {to} is not SeaDrop"
    if v != 0:
        return False, f"value {v} != 0"
    # NOTE: OpenSea appends a short attribution suffix (measured: 4 bytes) after the ABI body,
    # so the body is NOT a multiple of 32 bytes. Validate by real ABI decoding instead.
    if not re.fullmatch(r"0x[0-9a-f]+", data) or len(data) % 2 or len(data) < 10 + 64 * 4:
        return False, "calldata malformed"
    sel = data[:10]
    if sel != SEL_SIGNED:
        return False, f"selector {sel} is not mintSigned (public stage costs 0.1 ETH — refused)"
    if len(data) < 10 + 64 * 14:
        return False, "mintSigned calldata too short"
    try:
        dec = abi_decode(["address", "address", "address", "uint256",
                          "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "uint256", "bytes"],
                         bytes.fromhex(data[10:]))
    except Exception as e:
        return False, f"ABI decode failed: {str(e)[:60]}"
    if len(dec[6]) != 65:
        return False, f"signature length {len(dec[6])} != 65"
    abi_end = 32 * 14 + 32 * ((65 + 31) // 32)               # head(13 words)+len word+sig padded = 544 bytes
    if (len(data) - 10) // 2 - abi_end > 64:
        return False, "unexpected trailing data > 64 bytes"
    if int(word(data, 0), 16) != int(NFT, 16):
        return False, "nft arg mismatch"
    if int(word(data, 2), 16) not in (0, int(me, 16)):
        return False, "recipient is not this wallet"
    if int(word(data, 3), 16) != 1:
        return False, f"quantity {int(word(data, 3), 16)} != 1"
    if int(word(data, 4), 16) != 0:
        return False, f"signed mintPrice {int(word(data, 4), 16)} != 0"
    return True, "mintSigned"


def signed_window(data):
    d = data.lower()
    return int(word(d, 6), 16), int(word(d, 7), 16)


def _safe(f):
    try:
        return f()
    except Exception:
        return None


def api_mint(sess, apikey, me):
    r = sess.post(f"{API_BASE}/{SLUG}/mint", headers={"X-API-KEY": apikey},
                  json={"minter": me, "quantity": 1}, timeout=6)
    rem = r.headers.get("x-resource-ratelimit-remaining")
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    ra = r.headers.get("retry-after")
    RL.retry_after = float(ra) if ra and ra.replace(".", "").isdigit() else None
    return r.status_code, body, (int(rem) if rem is not None and rem.lstrip("-").isdigit() else None)


class RL:             # last retry-after seen from OpenSea
    retry_after = None


def rr(method, params, tries=8):
    """Robust read: retry every READ_RPC until a non-null result; raises only after ~3 s of total failure."""
    last = None
    for _ in range(tries):
        for u in READ_RPCS:
            try:
                r = rpc(method, params, u, timeout=4)
                if r.get("result") is not None:
                    return r["result"]
                last = r.get("error")
            except Exception as e:
                last = e
        time.sleep(0.3)
    raise RuntimeError(f"{method} failed on all RPCs: {str(last)[:80]}")


def nft_balance(me):
    try:
        return int(rr("eth_call", [{"to": NFT, "data": "0x70a08231" + me[2:].lower().rjust(64, "0")}, "latest"], tries=3), 16)
    except Exception:
        return -1                                        # unknown; callers treat < 1 as 'not held'


_STAGE_RE = re.compile(r"active drop stage, '([^']+)'")


def active_stage_named(msg):
    """Name of the ACTIVE stage quoted by OpenSea in a 422 ('...active drop stage, 'X' ...'), else None."""
    m_ = _STAGE_RE.search(msg)
    return m_.group(1) if m_ else None


def other_stage_active(msg):
    """422 that names a DIFFERENT active stage (e.g. 'Team Mint' right before T0) == ours is not active yet."""
    n = active_stage_named(msg)
    return n is not None and n != STAGE_LABEL


def precise_sleep_until(ts):
    while True:
        rem = ts - time.time()
        if rem <= 0:
            return
        if rem > 0.05:
            time.sleep(rem - 0.03)


def l1_component_estimate(n_bytes=584):
    NI = "0x00000000000000000000000000000000000000C8"
    sel = "0x" + keccak(text="gasEstimateL1Component(address,bool,bytes)")[:4].hex()
    data = sel + abi_encode(["address", "bool", "bytes"], [to_checksum_address(SD), False, b"\xab" * n_bytes]).hex()
    x = rpc("eth_call", [{"to": NI, "data": data}, "latest"])["result"][2:]
    return int(x[:64], 16)


def api_stage_times(apikey):
    d = _main_s.get(f"{API_BASE}/{SLUG}", headers={"X-API-KEY": apikey}, timeout=6).json()
    for st in d.get("stages", []):
        if st.get("label") == STAGE_LABEL:
            p = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()
            return int(p(st["start_time"])), int(p(st["end_time"])), st, d
    raise RuntimeError(f"stage {STAGE_LABEL!r} not found in API")


def broadcast(raw, sessions):
    """Send the same signed tx to every endpoint in parallel; return per-endpoint result."""
    res = {}
    def f(u, s):
        t = time.time()
        try:
            r = s.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw]}, timeout=4).json()
        except Exception as e:
            r = {"error": {"message": f"exc {str(e)[:60]}"}}
        r["_ms"] = round((time.time() - t) * 1000)
        res[u] = r
    ths = [threading.Thread(target=f, args=(u, s), daemon=True) for u, s in sessions.items()]
    [t.start() for t in ths]
    # return as soon as ANY endpoint accepted (the sequencer orders it; slower RPCs keep running in
    # the background), otherwise wait until every endpoint answered or 4.5 s passed
    end = time.time() + 4.5
    while time.time() < end:
        snap = dict(res)
        if any(accepted(r) for r in snap.values()) or all(not t.is_alive() for t in ths):
            return snap
        time.sleep(0.002)
    return dict(res)


def accepted(r):
    if "result" in r:
        return True
    m = json.dumps(r.get("error", "")).lower()
    return "already known" in m or "already exists" in m


def wait_receipt(h, sessions, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        for u, s in sessions.items():
            try:
                rc = s.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [h]}, timeout=2).json().get("result")
                if rc:
                    return rc
            except Exception:
                pass
        time.sleep(0.05)
    return None


def wait_stage_active(sessions, apikey, win, t_end, period=0.12):
    """Two staggered pollers on GET /drops/{slug}; returns True as soon as active_stage == STAGE_LABEL."""
    flag = threading.Event()
    t_start = time.time()
    def poll(s, delay):
        time.sleep(delay)
        while not flag.is_set() and not win.is_set() and time.time() < t_end:
            try:
                lab = (s.get(f"{API_BASE}/{SLUG}", headers={"X-API-KEY": apikey}, timeout=3).json().get("active_stage") or {}).get("label")
            except Exception:
                lab = None
            if lab == STAGE_LABEL:
                flag.set(); return
            # read bucket is 600/h: fast for the first 20 s, then 1 req/s per poller
            time.sleep(period if time.time() - t_start < 20 else 1.0)
    ths = [threading.Thread(target=poll, args=(s, i * period / 2), daemon=True) for i, s in enumerate(sessions)]
    [t.start() for t in ths]
    t0 = time.time()
    while not flag.wait(0.01):
        if win.is_set():
            return True
        if time.time() > t_end:
            return False
        if time.time() - t0 > 2 and int((time.time() - t0) * 100) % 1000 == 0:
            log("still waiting for OpenSea to activate the stage ...")
    log(f"read API: '{STAGE_LABEL}' is ACTIVE (waited {time.time()-t0:.2f}s) -> firing mint request")
    return True


# ---------------------------------------------------------------- main
def main():
    global SEND_RPCS, READ_RPCS
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", default=os.environ.get("MINT_KEY_FILE", "/root/mint-wallet.env"))
    ap.add_argument("--arm", action="store_true", help="really broadcast")
    ap.add_argument("--start", type=float, help="override T0 (testing only)")
    ap.add_argument("--send-rpc", action="append", help="override send RPCs (testing only)")
    ap.add_argument("--read-rpc", action="append", help="override read RPCs (testing only)")
    ap.add_argument("--no-api-check", action="store_true", help="testing only")
    a = ap.parse_args()
    if a.send_rpc:
        SEND_RPCS = a.send_rpc
    if a.read_rpc:
        READ_RPCS = a.read_rpc

    acct = Account.from_key(load_key(a.key_file))
    me = acct.address
    keys = load_api_keys()
    log(f"AERENT OG BOT v2  wallet={me[:6]}...{me[-4:]}  mode={'ARMED' if a.arm else 'DRY-RUN'}  api-keys={len(keys)}")

    if int(rpc("eth_chainId", [])["result"], 16) != CHAIN_ID:
        raise SystemExit("wrong chain")
    if rpc("eth_getCode", [me, "latest"])["result"] not in ("0x", "0x0"):
        raise SystemExit("wallet is a contract account")
    if nft_balance(me) >= TARGET_HELD:
        log("wallet already holds an AERENT OG Pass — nothing to do"); return

    T0, T_END = (a.start, a.start + 3600) if a.start else (STAGE_START, STAGE_END)
    if not a.no_api_check:
        s0, e0, st, d = api_stage_times(keys[0])
        log(f"API: stage '{STAGE_LABEL}' {st['start_time']} -> {st['end_time']} price={st['price']} max/wallet={st['max_per_wallet']} supply {d.get('total_supply')}/{d.get('max_supply')}")
        if st["price"] != "0":
            raise SystemExit("stage price is not 0 — refusing")
        if not a.start and (s0, e0) != (STAGE_START, STAGE_END):
            log(f"!! API times differ from built-in -> following API: {s0} -> {e0}")
            T0, T_END = s0, e0

    # ---- watchdog: re-read stage times every 60 s until T-45 s
    while T0 - time.time() > 45:
        rem = T0 - time.time()
        log(f"T-{rem/60:.1f} min")
        time.sleep(max(1, min(60, rem - 44)))
        if not a.no_api_check and not a.start and T0 - time.time() > 40:
            try:
                s0, e0, _, _ = api_stage_times(keys[0])
                if (s0, e0) != (T0, T_END):
                    log(f"!! STAGE MOVED by creator: {T0}->{s0}, end {T_END}->{e0}. Re-scheduling.")
                    T0, T_END = s0, e0
            except Exception as e:
                log(f"watchdog read failed ({str(e)[:60]}) — keeping T0")

    # ---- T-45 s: fees, gas, nonce (all fixed before T0)
    bal = int(rr("eth_getBalance", [me, "latest"]), 16)
    n_latest = int(rr("eth_getTransactionCount", [me, "latest"]), 16)
    n_pend = int(rr("eth_getTransactionCount", [me, "pending"]), 16)
    nonce = n_pend
    if n_pend != n_latest:
        log(f"!! a tx is pending (latest {n_latest} / pending {n_pend})")
    base = int(rr("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    try:
        l1 = l1_component_estimate()
    except Exception:
        l1 = 206_247                                     # measured on the Thousand mint
    need_min = 140_000 + 3 * l1                          # L2 execution ~133k measured + 3x L1 headroom
    gas = max(GAS_LIMIT, 140_000 + 4 * l1)
    max_fee = min(MAX_FEE_CAP, int(bal * 0.95) // gas)
    if max_fee < 4 * base:
        gas = max(need_min, int(bal * 0.95) // (4 * base))
        max_fee = min(MAX_FEE_CAP, int(bal * 0.95) // gas)
    if max_fee < 2 * base or gas < need_min:
        raise SystemExit(f"balance {bal/1e18:.8f} ETH too low for safe gas")
    PRIO = max_fee if PRIO_EQUALS_MAXFEE else 0
    log(f"balance {bal/1e18:.8f} ETH | baseFee {base/1e9:.4f} gwei | L1 est {l1} | gas {gas} | "
        f"maxFee {max_fee/1e9:.4f} gwei ({max_fee/max(base,1):.0f}x base) | tip {PRIO/1e9:.4f} gwei | "
        f"worst-case reserve {gas*max_fee/1e18:.6f} ETH | nonce {nonce}")

    # ---- T-3 s: fresh connections, warmed
    precise_sleep_until(T0 - 3.0)
    read_api_sess = [new_session(), new_session()]
    api_sess = [new_session() for _ in range(12)]     # one warm connection per in-flight request (Session is not thread-safe)
    send_sess = {u: new_session() for u in SEND_RPCS}
    read_sess = {u: new_session() for u in READ_RPCS}
    ths = []
    for i, s in enumerate(api_sess):
        k = keys[i % len(keys)]
        ths.append(threading.Thread(target=lambda s=s, k=k: _safe(lambda: s.get(f"{API_BASE}/{SLUG}", headers={"X-API-KEY": k}, timeout=3))))
    for s in read_api_sess:
        ths.append(threading.Thread(target=lambda s=s: _safe(lambda: s.get(f"{API_BASE}/{SLUG}", headers={"X-API-KEY": keys[0]}, timeout=3))))
    for u, s in list(send_sess.items()) + list(read_sess.items()):
        ths.append(threading.Thread(target=lambda u=u, s=s: _safe(lambda: s.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=3))))
    [t.start() for t in ths]
    [t.join(3) for t in ths]
    acct.sign_transaction({"to": to_checksum_address(SD), "value": 0, "data": "0x00", "nonce": nonce, "chainId": CHAIN_ID,
                           "gas": gas, "type": 2, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": PRIO})  # warm-up, never sent
    log("connections warm, waiting for T0")

    # ---- T0: hedged wave
    state = {"tx": None, "inflight": 0, "last_err": "", "not_on_list": 0}
    win = threading.Event()
    lk = threading.Lock()

    active = {"k": 0, "rem": None, "rl": False}

    def ask(slot, _kidx, tag):
        s = api_sess[slot % len(api_sess)]
        k = keys[active["k"]]
        t = time.time()
        try:
            code, body, rem = api_mint(s, k, me)
        except Exception as e:
            log(f"{tag} API exc {str(e)[:60]}"); return
        ms = (time.time() - t) * 1000
        if code == 200:
            ok, why = validate(body, me)
            if ok:
                with lk:
                    if state["tx"] is None:
                        state["tx"] = body
                        win.set()
                        log(f"{tag} API 200 in {ms:.0f} ms -> calldata valid (WIN)")
                    else:
                        log(f"{tag} API 200 in {ms:.0f} ms (late, ignored)")
            else:
                log(f"{tag} API 200 but REFUSED: {why}")
                with lk:
                    state["last_err"] = "REFUSED:" + why
        else:
            msg = json.dumps(body)[:260]
            with lk:
                state["last_err"] = msg
                active["rem"] = rem
                active["rl"] = code == 429 or (rem is not None and rem <= 0)
                if code in (401, 403) and active["k"] + 1 < len(keys):
                    active["k"] += 1
                    log(f"{tag} key rejected ({code}) -> failover to key #{active['k']}")
                named = active_stage_named(msg) if code == 422 else None
                if named == STAGE_LABEL and "allowlist" in msg:
                    state["not_on_list"] += 1
                if named is not None and named != STAGE_LABEL:
                    state["last_err"] = "not currently active (other stage still active: " + named + ")"
                if named == STAGE_LABEL and "limit" in msg.lower():
                    state["limit_hit"] = True
            log(f"{tag} API {code} in {ms:.0f} ms rem={rem}: {msg}")

    precise_sleep_until(T0)
    log(">>> T0")
    t_fire = time.time()
    for i, (off, kidx) in enumerate(WAVE):
        if win.wait(max(0.0, t_fire + off - time.time())):     # wakes instantly on a win
            break
        threading.Thread(target=ask, args=(i, kidx, f"w{i}"), daemon=True).start()

    # follow-up retries if the wave didn't win: paced, alternating keys, bounded by budget
    retry = 0
    while not win.wait(0.5 if retry < 2 else 2.0):
        if time.time() > T_END:
            log("stage ended without a signature"); return
        if state["last_err"].startswith("REFUSED"):
            log("API returned calldata that failed validation — stopping for safety"); return
        if state.get("limit_hit"):
            if nft_balance(me) >= TARGET_HELD:
                log("*** wallet already holds the NFT (API: wallet limit reached) ***"); return
            state["limit_hit"] = False
        if state["not_on_list"] >= 3:
            if not state.get("warned"):
                state["warned"] = True
                log("!! OpenSea says this wallet is NOT on the OG List allowlist (3x). No gas spent. "
                    "Switching to slow re-check every 20 s in case it was a transient backend error.")
            if win.wait(20):
                break
        if "not currently active" in state["last_err"]:
            # Stage not open yet on OpenSea's side: do NOT burn the 5-call mint budget.
            # Poll the read endpoint (separate 600/h bucket) with 2 staggered threads until it flips.
            if not wait_stage_active(read_api_sess, keys[active["k"]], win, T_END):
                log("stage never became active"); return
            with lk:
                state["last_err"] = ""
            threading.Thread(target=ask, args=(len(WAVE) + retry % (12 - len(WAVE)), 0, f"r{retry}"), daemon=True).start()
            retry += 1
            if win.wait(0.25):
                break
            threading.Thread(target=ask, args=(len(WAVE) + retry % (12 - len(WAVE)), 0, f"r{retry}"), daemon=True).start()
            retry += 1
            continue
        if active["rl"]:
            w = min(RL.retry_after or 15, 30)
            log(f"API budget exhausted -> waiting {w:.0f}s (respecting OpenSea rate limit)")
            if win.wait(w):
                break
            active["rl"] = False
        threading.Thread(target=ask, args=(len(WAVE) + retry % (12 - len(WAVE)), 0, f"r{retry}"), daemon=True).start()
        retry += 1

    tx = state["tx"]
    st_, en_ = signed_window(tx["data"])
    log(f"signed window {st_}->{en_}  now={time.time():.3f}")
    if time.time() > en_:
        log("signed window already closed"); return
    precise_sleep_until(st_ + FIRE_GUARD_S)

    if not a.arm:
        sim = rpc("eth_call", [{"from": me, "to": SD, "data": tx["data"], "value": "0x0"}, "latest"])
        log(f"DRY-RUN: eth_call -> {'OK' if 'result' in sim else json.dumps(sim.get('error'))[:200]}")
        log(f"DRY-RUN: total T0->ready {(time.time()-T0)*1000:.0f} ms. Not broadcasting.")
        return

    broadcasts = 0
    while broadcasts < MAX_BROADCASTS and time.time() < en_:
        signed = acct.sign_transaction({"to": to_checksum_address(SD), "value": 0, "data": tx["data"], "nonce": nonce,
                                        "chainId": CHAIN_ID, "gas": gas, "type": 2,
                                        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": PRIO})
        raw = signed.raw_transaction.to_0x_hex()
        h = "0x" + signed.hash.hex().removeprefix("0x")
        res = broadcast(raw, send_sess)
        broadcasts += 1
        log(f"SENT #{broadcasts} nonce {nonce} {h} (T0+{(time.time()-T0)*1000:.0f} ms) | " +
            " | ".join(f"{u.split('//')[1][:14]} {r.get('_ms','?')}ms {'ok' if accepted(r) else str(r.get('error',{}).get('message','?'))[:60]}" for u, r in res.items()))
        if not any(accepted(r) for r in res.values()):
            msgs = json.dumps(res).lower()
            if "nonce too low" in msgs:
                if nft_balance(me) >= TARGET_HELD:
                    log("*** wallet holds the NFT ***"); return
                try:
                    nonce = int(rr("eth_getTransactionCount", [me, "pending"]), 16)
                    log(f"nonce refreshed -> {nonce}")
                except Exception as e:
                    log(f"nonce refresh failed: {str(e)[:60]}")
            time.sleep(0.15); continue
        rc = wait_receipt(h, read_sess)
        if rc and int(rc["status"], 16) == 1:
            log(f"*** MINTED *** block {int(rc['blockNumber'],16)} idx {int(rc['transactionIndex'],16)} gasUsed {int(rc['gasUsed'],16)} | holds {nft_balance(me)}")
            return
        if nft_balance(me) >= TARGET_HELD:
            log("*** wallet holds the NFT ***"); return
        try:
            sim = rpc("eth_call", [{"from": me, "to": SD, "data": tx["data"], "value": "0x0"}, "latest"])
        except Exception as e:
            sim = {"error": {"message": f"sim exc {str(e)[:60]}"}}
        err = json.dumps(sim.get("error", ""))[:220]
        log(f"tx {'REVERTED' if rc else 'not mined in 20 s'}; eth_call now: {'OK' if 'result' in sim else err}")
        if not rc and "result" in sim:
            continue                                  # not mined yet but still valid: resend same nonce
        if rc:
            nonce += 1
        if "result" not in sim:
            low = err.lower()
            # 0xc0e1b4c5 etc. are custom errors; stop on anything that will not change by retrying
            if any(x in low for x in ("exceeds", "maxsupply", "max_supply", "invalid signature", "signatureal", "notactive")):
                log("revert reason will not fix itself — stopping."); return
            log("unknown revert — stopping to avoid burning gas."); return
    log(f"stopped. holds={nft_balance(me)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
    except SystemExit as e:
        log(f"exit: {e}")
        raise
    except Exception as e:
        log(f"fatal {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
