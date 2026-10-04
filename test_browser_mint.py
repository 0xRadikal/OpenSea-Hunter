import json
import threading
import urllib.request
import unittest

import browser_mint as bm


class BrowserMintTests(unittest.TestCase):
    def test_wallet_and_tx_shape(self):
        wallet = bm.validate_wallet("0x000000000000000000000000000000000000dEaD")
        tx = bm.build_tx(wallet, "0x1234")
        self.assertEqual(set(tx), {"from", "to", "value", "data"})
        self.assertEqual(tx["from"], wallet)
        self.assertEqual(tx["value"], "0x0")
        self.assertEqual(tx["data"], "0x1234")

    def test_callback_token(self):
        state = bm.CallbackState("session")
        self.assertFalse(state.submit("wrong", tx_hash="0x1"))
        self.assertTrue(state.submit("session", tx_hash="0xabc"))
        self.assertEqual(state.wait(0.1), ("0xabc", None))

    def test_local_page_and_callback(self):
        state = bm.CallbackState("session")
        tx = bm.build_tx("0x000000000000000000000000000000000000dEaD", "0x1234")
        server = bm.start_server(tx, state, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            page = urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/?token=session", timeout=3
            ).read().decode()
            self.assertIn("eth_sendTransaction", page)
            self.assertIn("0x1234", page)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/callback",
                data=json.dumps({"token": "session", "tx_hash": "0xfeed"}).encode(),
                headers={"content-type": "application/json"},
                method="POST",
            )
            self.assertEqual(urllib.request.urlopen(request, timeout=3).read(), b"ok")
            self.assertEqual(state.wait(0.1), ("0xfeed", None))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
