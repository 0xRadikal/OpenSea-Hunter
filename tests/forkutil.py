"""Fork setup shared by service fork tests. Talks ONLY to the local anvil fork."""
import os
import subprocess
import time

import requests
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F = "http://127.0.0.1:8547"
SD = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
NFT = "0xa655bbe47a8dec02991452adc7bef76bbbeceee9"       # real AERENT contract (fork pinned pre-sellout)
OWNER = "0xc7a08eac09cd3f3a699f2b575052226faca3c8c1"
FEE = "0x0000a26b00c1f0df003000390027140000faa719"
SEL_SIGNED = "0x" + keccak(text="mintSigned(address,address,address,uint256,(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)")[:4].hex()


def r(m, p):
    x = requests.post(F, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=30).json()
    if "error" in x:
        raise RuntimeError(f"{m}: {x['error']}")
    return x["result"]


def start_fork():
    subprocess.run([os.path.join(ROOT, "fork.sh")], check=True, stdout=subprocess.DEVNULL)
    r("anvil_setTime", [int(time.time())])
    r("evm_mine", [])


def stop_fork():
    subprocess.run([os.path.join(ROOT, "fork.sh"), "stop"], stdout=subprocess.DEVNULL)


def register_test_signer():
    ts = Account.create()
    now = int(time.time())
    r("anvil_impersonateAccount", [OWNER])
    r("anvil_setBalance", [OWNER, hex(10 ** 18)])
    d = "0x" + keccak(text="updateSignedMintValidationParams(address,address,(uint80,uint24,uint40,uint40,uint40,uint16,uint16))")[:4].hex() + \
        encode(["address", "address", "(uint80,uint24,uint40,uint40,uint40,uint16,uint16)"],
               [SD, ts.address, (0, 1000, now - 3600, now + 30 * 86400, 1000, 1000, 1000)]).hex()
    h = r("eth_sendTransaction", [{"from": OWNER, "to": NFT, "data": d, "gas": hex(500000)}])
    for _ in range(50):
        rc = r("eth_getTransactionReceipt", [h])
        if rc:
            assert rc["status"] == "0x1"
            return ts
        time.sleep(0.2)
    raise RuntimeError("signer setup not mined")


def signed_calldata(ts, minter, salt, start=None, end=None, price=0):
    import sys
    sys.path.insert(0, ROOT)
    from eip712 import digest
    now = int(time.time())
    mp = (price, 1, start or now - 60, end or now + 3600, 2, 1000, 1000, True)
    sig = ts.unsafe_sign_hash(digest(NFT, minter, FEE, mp, salt)).signature
    return SEL_SIGNED + encode(["address", "address", "address", "uint256", "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "uint256", "bytes"],
                               [NFT, FEE, "0x" + "0" * 40, 1, mp, salt, sig]).hex()


def funded_wallet(eth=10 ** 16):
    a = Account.create()
    r("anvil_setBalance", [a.address, hex(eth)])
    return a, "0x" + a.key.hex().removeprefix("0x")


def holds(addr):
    return int(r("eth_call", [{"to": NFT, "data": "0x70a08231" + addr[2:].lower().rjust(64, "0")}, "latest"]), 16)
