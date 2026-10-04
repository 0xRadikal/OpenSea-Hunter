"""Fork-only test helpers. Everything here talks to a local anvil fork (127.0.0.1) only."""
import os, tempfile
import requests
from eth_account import Account

FORK = "http://127.0.0.1:8547"


def r(method, params):
    assert FORK.startswith("http://127.0.0.1"), "fork helpers must only talk to a local fork"
    x = requests.post(FORK, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30).json()
    if "error" in x:
        raise RuntimeError(f"{method}: {x['error']}")
    return x["result"]


def throwaway_wallet(eth=10 ** 16):
    """Fresh random EOA funded ON THE FORK ONLY. Its key is written to a 0600 temp file that is
    deleted when the process exits. Tests never need (or read) a real wallet key."""
    acct = Account.create()
    r("anvil_setBalance", [acct.address, hex(eth)])
    fd, path = tempfile.mkstemp(prefix="forkkey_", suffix=".env")
    os.write(fd, f"PK={acct.key.hex() if acct.key.hex().startswith('0x') else '0x' + acct.key.hex()}\n".encode())
    os.close(fd)
    os.chmod(path, 0o600)
    import atexit
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return path, acct.address
