"""EIP-712 digest for SeaDrop SignedMint (used only by the fork tests to sign with a TEST signer)."""
from eth_abi import encode
from eth_utils import keccak

SD = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"
MP_T = ("MintParams(uint256 mintPrice,uint256 maxTotalMintableByWallet,uint256 startTime,uint256 endTime,"
        "uint256 dropStageIndex,uint256 maxTokenSupplyForStage,uint256 feeBps,bool restrictFeeRecipients)")
SM_T = "SignedMint(address nftContract,address minter,address feeRecipient,MintParams mintParams,uint256 salt)" + MP_T


def digest(nft, minter, fee, mp, salt, chain=4663):
    dom = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"],
                        [keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                         keccak(text="SeaDrop"), keccak(text="1.0"), chain, SD]))
    mph = keccak(encode(["bytes32"] + ["uint256"] * 7 + ["bool"], [keccak(text=MP_T)] + list(mp)))
    sh = keccak(encode(["bytes32", "address", "address", "address", "bytes32", "uint256"],
                       [keccak(text=SM_T), nft, minter, fee, mph, salt]))
    return keccak(b"\x19\x01" + dom + sh)
