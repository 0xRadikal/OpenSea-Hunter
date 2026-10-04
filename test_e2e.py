# End-to-end DRY-RUN with mocked OpenSea mint endpoint (no API budget used, nothing broadcast)
import sys, time, importlib.util
from eth_abi import encode
sp=importlib.util.spec_from_file_location("b","thousand_mint.py"); b=importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
FEE="0x0000a26b00c1f0df003000390027140000faa719"
now=int(time.time())
calls={"n":0}
def fake(apikey, me):
    calls["n"]+=1
    if calls["n"]==1:
        return 422, {"errors":["Wallet is not eligible for the active drop stage, 'TEAM' ..."]}, 3, None
    if calls["n"]==2:
        return 429, {"errors":["rate"]}, 0, int(time.time())+300   # forces key switch
    d=b.SEL_SIGNED+encode(["address","address","address","uint256","(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)","uint256","bytes"],
        [b.NFT,FEE,"0x"+"0"*40,1,(0,1,now-5,now+3600,1,1000,1000,True),1,b"\x11"*65]).hex()
    return 200, {"chain":"robinhood","to":b.SD,"data":d,"value":"0x0"}, 2, None
b.api_mint=fake
sys.argv=["x","--start",str(time.time()+0.5),"--stage-label","TEAM"]
b.main()
print("mock calls",calls["n"])
