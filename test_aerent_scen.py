import sys, time, importlib.util, requests
F="http://127.0.0.1:8547"
sp=importlib.util.spec_from_file_location("b","aerent_mint.py"); b=importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
from eth_abi import encode
FEE="0x0000a26b00c1f0df003000390027140000faa719"
scen=sys.argv[1]
n={"c":0}
def bad_sig_cd():
    mp=(0,1,int(time.time())-60,int(time.time())+3600,2,1000,1000,True)
    return b.SEL_SIGNED+encode(["address","address","address","uint256","(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)","uint256","bytes"],[b.NFT,FEE,"0x"+"0"*40,1,mp,5,b"\x22"*65]).hex()
def fake(sess,key,me):
    n["c"]+=1
    if scen=="notlisted":
        return 422,{"errors":["Wallet is not eligible for the active drop stage, 'OG List' (signed_presale): the wallet is not on this stage's allowlist."]},3
    if scen=="badsig":
        return 200,{"to":b.SD,"data":bad_sig_cd(),"value":"0x0"},3
    if scen=="public":
        return 200,{"to":b.SD,"data":b.SEL_PUBLIC+encode(["address","address","address","uint256"],[b.NFT,FEE,"0x"+"0"*40,1]).hex(),"value":hex(10**17)},3
    if scen=="429":
        return (429,{"errors":["rate"]},0) if n["c"]<9 else (422,{"errors":["...'OG List'... not on this stage's allowlist"]},2)
b.api_mint=fake
r=lambda m,p: requests.post(F,json={"jsonrpc":"2.0","id":1,"method":m,"params":p}).json()["result"]
r('anvil_setTime',[int(time.time())]); r('evm_mine',[])
from fork_helpers import throwaway_wallet; KEYFILE, me = throwaway_wallet()
n0=int(r("eth_getTransactionCount",[me,"latest"]),16); bal0=int(r("eth_getBalance",[me,"latest"]),16)
sys.argv=["x","--arm","--key-file",KEYFILE,"--no-api-check","--start",str(time.time()+3.5),"--send-rpc",F,"--read-rpc",F]
t=time.time()
try: b.main()
except SystemExit as e: print("SystemExit",e)
n1=int(r("eth_getTransactionCount",[me,"latest"]),16); bal1=int(r("eth_getBalance",[me,"latest"]),16)
print(f"SCENARIO {scen}: api calls={n['c']} txs sent={n1-n0} gas spent={(bal0-bal1)/1e18:.9f} ETH elapsed={time.time()-t:.1f}s")
