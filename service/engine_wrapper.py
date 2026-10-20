#!/usr/bin/env python3
"""Run the mint engine (mint_engine.py = aerent_mint.py + mint policy) for one service task.

Only module-level target constants are overridden (the same technique as run_testdrop.py, which minted on
mainnet). Secrets arrive as ONE JSON line on stdin -> never on argv, in env, or on disk:
    {"pk": "0x...", "api_key": "..."}
The engine masks both in its log via its own _SECRETS list.
Last stdout line:  OSH_RESULT {"holds": n, "target": m, "error": "..."}
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_engine():
    sp = importlib.util.spec_from_file_location("aerent_engine", os.path.join(HERE, "mint_engine.py"))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", type=int, required=True)        # for /proc identification only
    ap.add_argument("--slug", required=True)
    ap.add_argument("--nft", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--kind", choices=("signed", "public"), required=True)
    ap.add_argument("--price-wei", type=int, required=True)      # price the user confirmed
    ap.add_argument("--max-value-wei", type=int, required=True)  # hard spend cap
    ap.add_argument("--dry-run", action="store_true")
    # testing only (fork + fake OpenSea); never used by the service in production
    ap.add_argument("--api-base")
    ap.add_argument("--rpc", action="append")
    ap.add_argument("--no-api-check", action="store_true")
    a = ap.parse_args()

    line = sys.stdin.readline()
    sys.stdin.close()
    try:
        sec = json.loads(line)
        pk, api_key = sec["pk"], sec["api_key"]
    except Exception:
        print('OSH_RESULT {"holds": -1, "target": -1, "error": "bad secret input"}', flush=True)
        return 2
    del line, sec

    bot = load_engine()
    bot._SECRETS.extend([pk, pk[2:], api_key])
    bot.load_key = lambda _path: pk
    bot.load_api_keys = lambda *_a, **_k: [api_key]
    bot.SLUG, bot.NFT = a.slug, a.nft.lower()
    bot.STAGE_LABEL, bot.STAGE_START, bot.STAGE_END = a.label, a.start, a.end
    bot.MINT_KIND, bot.EXPECTED_PRICE, bot.MAX_VALUE_WEI = a.kind, a.price_wei, a.max_value_wei
    if a.api_base:
        bot.API_BASE = a.api_base.rstrip("/") + "/api/v2/drops"
    if a.rpc:
        bot.READ_RPCS = list(a.rpc)

    from eth_account import Account
    me = Account.from_key(pk).address
    held = bot.nft_balance(me)
    if held < 0:
        print('OSH_RESULT {"holds": -1, "target": -1, "error": "cannot read NFT balance"}', flush=True)
        return 3
    bot.TARGET_HELD = held + 1           # mint exactly one more than the wallet holds now

    argv = ["aerent_mint.py"] + ([] if a.dry_run else ["--arm"])
    for u in a.rpc or []:
        argv += ["--send-rpc", u, "--read-rpc", u]
    if a.no_api_check:
        argv += ["--no-api-check", "--start", str(a.start)]
    sys.argv = argv
    err = ""
    try:
        bot.main()
    except SystemExit as e:
        err = bot.mask(str(e)) if e.code not in (0, None) else ""
    except BaseException as e:  # noqa: BLE001 — report and exit
        err = bot.mask(f"{type(e).__name__}: {str(e)[:160]}")
    holds = bot.nft_balance(me)
    print("OSH_RESULT " + json.dumps({"holds": holds, "target": bot.TARGET_HELD, "error": err}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
