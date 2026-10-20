#!/usr/bin/env python3
"""Run the mint engine (mint_engine.py = aerent_mint.py + mint policy) for one service task.

Only module-level target constants are overridden (the same technique as run_testdrop.py, which minted on
mainnet). Secrets arrive as ONE JSON line on stdin -> never on argv, in env, or on disk:
    {"pk": "0x...", "api_key": "..."}
The engine masks both in its log via its own _SECRETS list.
Last stdout line:  OSH_RESULT {"minted": bool, "tx": hash|null, "token_ids": [...], "reason": str|null, "error": str,
                               "holds": n, "wanted": q, "quantity": q_effective|null}
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
    ap.add_argument("--max-value-wei", type=int, required=True)  # hard spend cap (whole tx)
    ap.add_argument("--quantity", type=int, default=1)           # tokens wanted in this stage (may only shrink)
    ap.add_argument("--nonce-file")                              # per-wallet nonce lock shared by sibling tasks
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
        print('OSH_RESULT ' + json.dumps({"minted": False, "tx": None, "token_ids": [], "reason": "bad_input", "error": "bad secret input", "holds": -1}), flush=True)
        return 2
    del line, sec

    bot = load_engine()
    bot._SECRETS.extend([pk, pk[2:], api_key])
    bot.load_key = lambda _path: pk
    bot.load_api_keys = lambda *_a, **_k: [api_key]
    bot.SLUG, bot.NFT = a.slug, a.nft.lower()
    bot.STAGE_LABEL, bot.STAGE_START, bot.STAGE_END = a.label, a.start, a.end
    bot.MINT_KIND, bot.EXPECTED_PRICE, bot.MAX_VALUE_WEI = a.kind, a.price_wei, a.max_value_wei
    bot.QUANTITY, bot.NONCE_FILE = a.quantity, a.nonce_file
    if a.api_base:
        bot.API_BASE = a.api_base.rstrip("/") + "/api/v2/drops"
    if a.rpc:
        bot.READ_RPCS = list(a.rpc)

    from eth_account import Account
    me = Account.from_key(pk).address

    def emit(**kw):
        out = {"minted": False, "tx": None, "token_ids": [], "reason": None, "error": "", "holds": -1,
               "wanted": a.quantity, "quantity": None}
        out.update(kw)
        print("OSH_RESULT " + json.dumps(out), flush=True)

    held = bot.nft_balance(me)
    if held < 0:
        emit(reason="rpc_unavailable", error="cannot read NFT balance")
        return 3
    bot.TARGET_HELD = held + a.quantity  # engine re-baselines this at T-45 s (after clamping the quantity)

    argv = ["mint_engine.py"] + ([] if a.dry_run else ["--arm"])
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
    r = dict(bot.RESULT)
    if not r["minted"] and not r["reason"]:
        r["reason"] = "error" if err else "not_minted"
    emit(minted=r["minted"], tx=r["tx"], token_ids=r["token_ids"], reason=r["reason"], error=err, quantity=r.get("quantity"),
         holds=bot.nft_balance(me))
    return 0


if __name__ == "__main__":
    sys.exit(main())
