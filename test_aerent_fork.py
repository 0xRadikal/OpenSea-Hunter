"""
FORK test (anvil fork of live Robinhood Chain). NOTHING is sent to mainnet:
  - SEND/READ RPCs forced to 127.0.0.1:8547 and asserted before main()
  - OpenSea API mocked
On the fork only: impersonate the real NFT owner, register a TEST signer on SeaDrop,
sign a genuine EIP-712 SignedMint for our wallet, and let the bot mint against the real contract.
"""
import os, sys, time, importlib.util, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eip712 import digest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
F = "http://127.0.0.1:8547"
def r(m, p): 
    x = requests.post(F, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=30).json()
    if "error" in x: raise RuntimeError(f"{m}: {x['error']}")
    return x["result"]
sp = importlib.util.spec_from_file_location("b", "aerent_mint.py"); b = importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
OWNER = "0xc7a08eac09cd3f3a699f2b575052226faca3c8c1"
FEE = "0x0000a26b00c1f0df003000390027140000faa719"
test_signer = Account.create()
r("anvil_setTime", [int(time.time())]); r("evm_mine", [])
now = int(r("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
print("fork block ts", now, "wall", int(time.time()))
# owner -> NFT.updateSignedMintValidationParams(seaDrop, signer, params)
r("anvil_impersonateAccount", [OWNER]); r("anvil_setBalance", [OWNER, hex(10**18)])
params = (0, 1000, now - 3600, 1791826200, 1000, 1000, 1000)
data = "0x" + keccak(text="updateSignedMintValidationParams(address,address,(uint80,uint24,uint40,uint40,uint40,uint16,uint16))")[:4].hex() + \
       encode(["address", "address", "(uint80,uint24,uint40,uint40,uint40,uint16,uint16)"], [b.SD, test_signer.address, params]).hex()
h = r("eth_sendTransaction", [{"from": OWNER, "to": b.NFT, "data": data, "gas": hex(500000)}])
rc = None
for _ in range(50):
    rc = r("eth_getTransactionReceipt", [h])
    if rc: break
    time.sleep(0.2)
print("setup signer tx status", rc["status"])
assert rc["status"] == "0x1"
from fork_helpers import throwaway_wallet
KEYFILE, me = throwaway_wallet()
b._SECRETS.clear()
mp = (0, 1, now - 60, now + 3600, 2, 1000, 1000, True)
salt = 0x1234
sig = test_signer.unsafe_sign_hash(digest(b.NFT, me, FEE, mp, salt)).signature
cd = b.SEL_SIGNED + encode(["address", "address", "address", "uint256", "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)", "uint256", "bytes"],
                           [b.NFT, FEE, "0x" + "0" * 40, 1, mp, salt, sig]).hex()
print("validator on test calldata:", b.validate({"to": b.SD, "data": cd, "value": "0x0"}, me))
calls = {"n": 0}
def fake(sess, key, minter):
    calls["n"] += 1
    if calls["n"] <= 2:
        return 409, {"errors": ["Drop is not currently active for minting"]}, 4   # simulate late stage flip
    return 200, {"chain": "robinhood", "to": b.SD, "data": cd, "value": "0x0"}, 3
b.api_mint = fake
b.wait_stage_active = lambda *a, **k: True   # read-API flip is covered by test_aerent_lateflip.py
bal0 = int(r("eth_getBalance", [me, "latest"]), 16)
sys.argv = ["x", "--arm", "--key-file", KEYFILE, "--no-api-check", "--start", str(time.time() + 4), "--send-rpc", F, "--read-rpc", F]
# hard safety assertion: verify nothing points to mainnet
assert all("127.0.0.1" in x for x in sys.argv if x.startswith("http")), "unsafe RPC"
b.main()
assert b.SEND_RPCS == [F] and b.READ_RPCS == [F], "RPC override failed"
bal1 = int(r("eth_getBalance", [me, "latest"]), 16)
hold = int(r("eth_call", [{"to": b.NFT, "data": "0x70a08231" + me[2:].lower().rjust(64, "0")}, "latest"]), 16)
print(f"FORK RESULT: holds={hold}  gas cost={(bal0-bal1)/1e18:.9f} ETH  api calls={calls['n']}")
sys.exit(0 if hold == 1 else 1)
