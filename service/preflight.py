"""Read-only checks before a task is accepted. Nothing here sends a transaction.

Scope (deliberately the same as the proven engine): Robinhood Chain, SeaDrop V1 ERC-721, FREE signed stages
(signed_presale, price 0). The engine hard-codes value=0 and refuses any paid / public calldata.
"""
import time

import requests

from .opensea import iso_ts

CHAIN_ID = 4663
SD = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
import os as _os
READ_RPCS = [u for u in _os.environ.get("OSH_READ_RPCS", "").split(",") if u] or \
    ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood.drpc.org"]
WARN_BALANCE_WEI = 100_000_000_000_000      # 0.0001 ETH; measured real mint cost 0.00003-0.00005 ETH (fork + mainnet logs)


class PreflightError(Exception):
    pass


def rpc(method, params, rpcs=None):
    last = None
    for u in rpcs or READ_RPCS:
        try:
            j = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8).json()
            if "result" in j:
                return j["result"]
            last = j.get("error")
        except Exception as e:  # noqa: BLE001
            last = e
    raise PreflightError(f"RPC {method} failed: {str(last)[:80]}")


ZERO = "0x" + "0" * 40


def _price(st):
    p = str(st.get("price") if st.get("price") is not None else "")
    return int(p) if p.isdigit() else None


def kind_of(st):
    """'signed' / 'public' if the engine can mint this stage, else None (with reason via unsupported_reason)."""
    return {"signed_presale": "signed", "public_sale": "public"}.get(st.get("stage_type"))


def unsupported_reason(s):
    if s["kind"] is None:
        return f"نوع مرحله «{s['type']}» پشتیبانی نمی‌شود"
    if s["price_wei"] is None:
        return "قیمت نامشخص است"
    if s["currency"] != ZERO:
        return "پرداخت با توکن ERC-20 پشتیبانی نمی‌شود"
    return None


def pick_stages(drop, now=None):
    """Stages that have not ended, earliest first (each tagged with kind / price / reason if unsupported)."""
    now = now or time.time()
    out = []
    for st in drop.get("stages") or []:
        try:
            s, e = iso_ts(st["start_time"]), iso_ts(st["end_time"])
        except Exception:
            continue
        if e <= now or not st.get("label"):
            continue
        out.append({"label": st["label"], "uuid": str(st.get("uuid")), "start": s, "end": e,
                    "type": st.get("stage_type"), "price": st.get("price"),
                    "max_per_wallet": st.get("max_per_wallet"),
                    "currency": (st.get("price_currency_address") or "0x" + "0" * 40).lower(),
                    "kind": kind_of(st),
                    "price_wei": _price(st)})
    return sorted(out, key=lambda x: x["start"])


def check_drop(drop):
    if drop.get("chain") != "robinhood":
        raise PreflightError(f"فعلاً فقط Robinhood Chain پشتیبانی می‌شود (این دراپ روی {drop.get('chain')} است)")
    if drop.get("drop_type") not in (None, "seadrop_v1_erc721"):
        raise PreflightError(f"نوع دراپ پشتیبانی نمی‌شود: {drop.get('drop_type')}")
    nft = (drop.get("contract_address") or "").lower()
    if len(nft) != 42:
        raise PreflightError("آدرس قرارداد در پاسخ OpenSea نیست")
    ms, ts = drop.get("max_supply"), drop.get("total_supply")
    if ms and ts and ms.isdigit() and ts.isdigit() and int(ts) >= int(ms):
        raise PreflightError("ظرفیت دراپ تمام شده (sold out)")
    return nft


def check_chain_contract(nft, rpcs=None):
    if int(rpc("eth_chainId", [], rpcs), 16) != CHAIN_ID:
        raise PreflightError("RPC روی شبکه اشتباه است")
    if rpc("eth_getCode", [nft, "latest"], rpcs) in ("0x", "0x0"):
        raise PreflightError("قرارداد NFT روی زنجیره کد ندارد")
    # SeaDrop must have a signer registered for this NFT (getSigners(address))
    res = rpc("eth_call", [{"to": SD, "data": "0x" + _sel("getSigners(address)") + nft[2:].rjust(64, "0")}, "latest"], rpcs)
    if len(res) < 2 + 64 * 2 or int(res[2 + 64: 2 + 128], 16) == 0:
        raise PreflightError("هیچ امضاکننده‌ای روی SeaDrop برای این NFT ثبت نشده (مینت امضادار ممکن نیست)")


def _sel(sig):
    from eth_utils import keccak
    return keccak(text=sig)[:4].hex()


def check_wallet(address, rpcs=None):
    if rpc("eth_getCode", [address, "latest"], rpcs) not in ("0x", "0x0"):
        raise PreflightError("ولت قرارداد هوشمند است؛ فقط EOA پشتیبانی می‌شود")
    bal = int(rpc("eth_getBalance", [address, "latest"], rpcs), 16)
    return bal


def nft_balance(nft, address, rpcs=None):
    return int(rpc("eth_call", [{"to": nft, "data": "0x70a08231" + address[2:].lower().rjust(64, "0")}, "latest"], rpcs), 16)


def onchain_public_price(nft, rpcs=None):
    res = rpc("eth_call", [{"to": SD, "data": "0x" + _sel("getPublicDrop(address)") + nft[2:].lower().rjust(64, "0")}, "latest"], rpcs)
    return int(res[2:66], 16)
