import os, pytest
from eth_account import Account
from service.vault import Vault, VaultError, normalize_private_key, create_master_key, load_master_key

def test_roundtrip_and_binding(tmp_path):
    v = Vault(os.urandom(32)); a = Account.create(); pk = "0x" + a.key.hex().removeprefix("0x")
    addr, blob = v.encrypt(7, pk)
    assert addr == a.address and pk.encode() not in blob and pk[2:].encode() not in blob
    assert v.decrypt(7, addr, blob) == pk.lower()
    with pytest.raises(VaultError): v.decrypt(8, addr, blob)                     # other user
    with pytest.raises(VaultError): v.decrypt(7, Account.create().address, blob)  # other address
    with pytest.raises(VaultError): Vault(os.urandom(32)).decrypt(7, addr, blob)  # other master
    bad = bytearray(blob); bad[-1] ^= 1
    with pytest.raises(VaultError): v.decrypt(7, addr, bytes(bad))                # tamper
    assert v.encrypt(7, pk)[1] != blob                                           # random nonce

@pytest.mark.parametrize("s", ["", "abc", "0x" + "0"*64, "0x" + "f"*64, "word " * 12, "0x" + "1"*63, "0x" + "g"*64])
def test_rejects(s):
    with pytest.raises(Exception): normalize_private_key(s)

def test_accepts_formats():
    a = Account.create(); h = a.key.hex().removeprefix("0x")
    for s in (h, "0x"+h, h.upper(), f' "0x{h}" '):
        assert normalize_private_key(s) == "0x" + h.lower()

def test_master_file(tmp_path):
    p = str(tmp_path / "m.key"); create_master_key(p)
    assert len(load_master_key(p)) == 32
    with pytest.raises(VaultError): create_master_key(p)
    os.chmod(p, 0o644)
    with pytest.raises(VaultError): load_master_key(p)

def test_seal_open():
    v = Vault(os.urandom(32)); b = v.seal(1, "apikey", "abc123")
    assert b"abc123" not in b and v.open(1, "apikey", b) == "abc123"
    with pytest.raises(VaultError): v.open(2, "apikey", b)
    with pytest.raises(VaultError): v.open(1, "other", b)
