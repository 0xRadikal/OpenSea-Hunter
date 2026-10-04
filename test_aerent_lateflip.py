# T0 -> mint API says 409 (not active). Read API flips to 'OG List' 2.0 s later. Mint API returns 200 after flip.
import os, sys, time, importlib.util, requests
MODE = sys.argv[1] if len(sys.argv) > 1 else "409"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from eip712 import digest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
F="http://127.0.0.1:8547"
sp=importlib.util.spec_from_file_location("b","aerent_mint.py"); b=importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
def r(m,p):
    x=requests.post(F,json={"jsonrpc":"2.0","id":1,"method":m,"params":p},timeout=30).json(); 
    if "error" in x: raise RuntimeError(x["error"])
    return x["result"]
OWNER="0xc7a08eac09cd3f3a699f2b575052226faca3c8c1"; FEE="0x0000a26b00c1f0df003000390027140000faa719"
ts=Account.create(); r("anvil_setTime",[int(time.time())]); r("evm_mine",[]); now=int(time.time())
r("anvil_impersonateAccount",[OWNER]); r("anvil_setBalance",[OWNER,hex(10**18)])
d="0x"+keccak(text="updateSignedMintValidationParams(address,address,(uint80,uint24,uint40,uint40,uint40,uint16,uint16))")[:4].hex()+encode(["address","address","(uint80,uint24,uint40,uint40,uint40,uint16,uint16)"],[b.SD,ts.address,(0,1000,now-3600,1791826200,1000,1000,1000)]).hex()
h=r("eth_sendTransaction",[{"from":OWNER,"to":b.NFT,"data":d,"gas":hex(500000)}]); time.sleep(1.5)
from fork_helpers import throwaway_wallet
KEYFILE, me = throwaway_wallet()
mp=(0,1,now-60,now+3600,2,1000,1000,True); sig=ts.unsafe_sign_hash(digest(b.NFT,me,FEE,mp,99)).signature
cd=b.SEL_SIGNED+encode(["address","address","address","uint256","(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)","uint256","bytes"],[b.NFT,FEE,"0x"+"0"*40,1,mp,99,sig]).hex()
T0=time.time()+4; FLIP=T0+2.0
calls={"mint":0,"read":0}
class FR:
    def __init__(s,j): s._j=j; s.status_code=200; s.headers={}
    def json(s): return s._j
class FS(requests.Session):
    def get(s,url,**k):
        if "api.opensea.io" in url:
            calls["read"]+=1
            return FR({"active_stage":{"label":"OG List"} if time.time()>=FLIP else None})
        raise RuntimeError("unexpected GET "+url)
    def post(s,url,**k):
        assert "127.0.0.1" in url, "NON-FORK POST BLOCKED: "+url
        return super().post(url,**k)
b.new_session=lambda: FS()
def fake(sess,key,minter):
    calls["mint"]+=1
    if time.time()<FLIP:
        if MODE=="team":
            return 422,{"errors":["Wallet is not eligible for the active drop stage, 'Team Mint' (signed_presale): the wallet is not on this stage's allowlist."]},4
        return 409,{"errors":["Drop is not currently active for minting"]},4
    return 200,{"to":b.SD,"data":cd,"value":"0x0"},3
b.api_mint=fake
sys.argv=["x","--arm","--key-file",KEYFILE,"--no-api-check","--start",str(T0),"--send-rpc",F,"--read-rpc",F]
b.main()
hold=int(r("eth_call",[{"to":b.NFT,"data":"0x70a08231"+me[2:].lower().rjust(64,"0")},"latest"]),16)
print(f"LATE-FLIP[{MODE}] RESULT holds={hold} mint-API calls={calls['mint']} read-API calls={calls['read']}")
