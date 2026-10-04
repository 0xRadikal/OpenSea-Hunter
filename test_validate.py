import sys; sys.argv=["x"]
import importlib.util
spec=importlib.util.spec_from_file_location("b","thousand_mint.py"); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
from eth_abi import encode
ME="0x7A7E000000000000000000000000000000000697"
FEE="0x0000a26b00c1f0df003000390027140000faa719"
def signed(nft=b.NFT,minter="0x"+"0"*40,q=1,price=0,st=1790699400,en=1790703000):
    body=encode(["address","address","address","uint256","(uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool)","uint256","bytes"],
                [nft,FEE,minter,q,(price,1,st,en,3,1000,1000,True),123,b"\x11"*65])
    return b.SEL_SIGNED+body.hex()
ok=lambda d,**k: b.validate(dict(to=k.get("to",b.SD),data=d,value=k.get("value","0x0")),ME)
cases=[("good signed",ok(signed()),True),
("good signed minter=me",ok(signed(minter=ME)),True),
("good public",ok(b.SEL_PUBLIC+encode(["address","address","address","uint256"],[b.NFT,FEE,"0x"+"0"*40,1]).hex()),True),
("wrong target",ok(signed(),to="0x"+"1"*40),False),
("value>0",ok(signed(),value="0x1"),False),
("value decimal>0",ok(signed(),value="5"),False),
("wrong nft",ok(signed(nft="0x"+"2"*40)),False),
("other recipient",ok(signed(minter="0x"+"3"*40)),False),
("qty 2",ok(signed(q=2)),False),
("price>0",ok(signed(price=10)),False),
("bad selector",ok("0xdeadbeef"+signed()[10:]),False),
("garbage",ok("0x12"),False)]
f=0
for n,(r,why),exp in cases:
    s="PASS" if r==exp else "FAIL"; f+=s=="FAIL"; print(s,n,"->",why)
d=signed().lower(); print("startTime word6",int(b.word(d,6),16),"endTime word7",int(b.word(d,7),16))
assert int(b.word(d,6),16)==1790699400 and int(b.word(d,7),16)==1790703000
print("FAILS",f); sys.exit(f)
