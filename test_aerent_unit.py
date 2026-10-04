import sys, importlib.util
from eth_abi import encode
sp=importlib.util.spec_from_file_location("b","aerent_mint.py"); b=importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
ME="0x7A7E000000000000000000000000000000000697"; FEE="0x0000a26b00c1f0df003000390027140000faa719"
def sg(nft=b.NFT,minter="0x"+"0"*40,q=1,price=0):
    return b.SEL_SIGNED+encode(["address","address","address","uint256","(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)","uint256","bytes"],
        [nft,FEE,minter,q,(price,1,1791045000,1791048600,2,1000,1000,True),7,b"\x11"*65]).hex()
V=lambda d,**k:b.validate(dict(to=k.get("to",b.SD),data=d,value=k.get("value","0x0")),ME)
pub=b.SEL_PUBLIC+encode(["address","address","address","uint256"],[b.NFT,FEE,"0x"+"0"*40,1]).hex()
C=[("signed ok",V(sg()),True),("signed minter=me",V(sg(minter=ME)),True),("uppercase to",V(sg(),to=b.SD.upper().replace("0X","0x")),True),
("PUBLIC refused (0.1 ETH stage)",V(pub),False),("public with value",V(pub,value=hex(10**17)),False),
("wrong to",V(sg(),to="0x"+"1"*40),False),("value>0",V(sg(),value="0x1"),False),("value dec",V(sg(),value="100000000000000000"),False),
("wrong nft",V(sg(nft="0x"+"2"*40)),False),("other minter",V(sg(minter="0x"+"3"*40)),False),("qty2",V(sg(q=2)),False),
("price>0",V(sg(price=1)),False),("bad sel",V("0xdeadbeef"+sg()[10:]),False),("truncated",V(sg()[:10+64*5]),False),
("odd len",V(sg()+"0"),False),("not dict",b.validate(None,ME),False),("missing data",b.validate({"to":b.SD},ME),False)]
f=0
for n,(r,w),e in C:
    ok=r==e; f+=not ok; print("PASS" if ok else "FAIL",n,"->",w)
# REAL OpenSea-generated calldata (has a 4-byte suffix after the ABI body)
import json as _j
real=_j.load(open("fixture_testdrop_mint.json")); nft0=b.NFT
b.NFT="0x728924f13327f17b5be06b00fd26a6df2a2aacb6"; r_=b.validate(real,"0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697"); print("PASS" if r_[0] else "FAIL","REAL test-drop calldata ->",r_[1]); f+=not r_[0]
b.NFT="0x0b10721af018aadfbb3e31d0f4d2dea2d6bd994b"; r_=b.validate({"to":b.SD,"data":open("fixture_thousand_calldata.txt").read(),"value":"0"},"0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697"); print("PASS" if r_[0] else "FAIL","REAL Thousand calldata ->",r_[1]); f+=not r_[0]
r_=b.validate({"to":b.SD,"data":open("fixture_thousand_calldata.txt").read()+"00"*80,"value":"0"},"0x7A7E856A3C37Ba9f088048f7240c6f3ff9f9d697"); print("PASS" if not r_[0] else "FAIL","real + 80B junk ->",r_[1]); f+=r_[0]
b.NFT=nft0
st,en=b.signed_window(sg()); assert (st,en)==(1791045000,1791048600); print("signed_window ok")
assert b.STAGE_START==1791045000 and b.STAGE_END==1791048600 and b.STAGE_LABEL=="OG List" and b.NFT=="0xa655bbe47a8dec02991452adc7bef76bbbeceee9"
print("constants ok"); print("FAILS",f); sys.exit(f)
