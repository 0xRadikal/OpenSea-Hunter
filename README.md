# OpenSea-Hunter

Allowlist (signed presale) mint bot for **OpenSea SeaDrop V1** drops on **Robinhood Chain** (chainId 4663).
It gets the mint signature from the **official OpenSea Drops API** for a wallet that is genuinely on the
allowlist, strictly validates it, and submits it as fast as the network allows.

> **Use responsibly.** This tool only mints for a wallet you control that OpenSea itself declares eligible.
> It respects OpenSea's rate limits (no key farming for throughput), and it cannot bypass allowlists.
> You are responsible for complying with OpenSea's Terms of Service and each project's rules.

## Track record (real mainnet results)

| Drop | Stage | Result | T0 → tx accepted |
|---|---|---|---|
| Thousand (`thousandart`) | WL | minted 1/1 | ~1.5 s (v1, `thousand_mint.py`) |
| Own test drop (`bottest-464316076`) | signed presale | minted (2 runs) | 0.6–1.2 s |
| AERENT OG Pass (`aerent`) | OG List | **minted token #273** (drop sold out 1000/1000 seconds later) | **0.61 s** |

## How it works (`aerent_mint.py`)

1. **Pre-flight.** It checks the chain id, refuses to run from a contract wallet, re-reads the stage times from the API every 60 s (and follows them if the creator moves the stage), and refuses any stage with a non-zero price.
2. **T-45 s.** It fixes the nonce, base fee and L1 gas estimate. The gas limit is 1.2M. In the Thousand rush, 161 of 204 failures came from gas limits of 200–300k, while every success used 350k or more. The priority fee is set equal to maxFee; on this chain the tip is never charged (verified on 380 txs), so this costs nothing.
3. **T-3 s.** It opens and warms one dedicated HTTP connection per request (OpenSea, sequencer, RPCs).
4. **T0.** It sends a staggered wave of `POST /api/v2/drops/{slug}/mint` requests. The first valid response wins.
   * If the stage is not active yet (`409 not currently active`, or a `422` naming another stage such as *Team Mint*), the bot polls the read endpoint, which has its own 600/h rate-limit bucket. It only spends the scarce mint budget (5 calls per key) once the stage is open.
5. **Strict validation** of OpenSea's calldata, by real ABI decoding. The bot only proceeds if all of these hold:
   * target is the SeaDrop contract
   * `value == 0`
   * the selector is `mintSigned`
   * the NFT is the correct one
   * the recipient is this wallet
   * quantity is 1
   * the signed price is 0
   * the signature is 65 bytes
   * trailing bytes are at most 64 (OpenSea appends a 4-byte suffix)
   
   A paid or public mint is never sent.
6. **Broadcast** the signed transaction in parallel to the sequencer endpoint, the official RPC and drpc. The bot continues as soon as any of them accepts it.
7. **Confirm** the receipt. If the tx reverts, the bot decodes the reason and stops when retrying cannot help. Gas-spending broadcasts are capped at 4.

## Setup

```bash
pip install -r requirements.txt
# secrets live OUTSIDE the repo, chmod 600
printf 'PK=0x...\n' > /root/mint-wallet.env && chmod 600 /root/mint-wallet.env
curl -s -X POST https://api.opensea.io/api/v2/auth/keys   # free instant API key
printf 'OPENSEA_API_KEYS=<key>\n' > /root/opensea-keys.env && chmod 600 /root/opensea-keys.env
```

The paths can be overridden with `MINT_KEY_FILE` and `OPENSEA_KEYS_FILE`. Use a **dedicated burner wallet**
funded with gas only, and move minted NFTs to a secure wallet afterwards.

## Configure a drop

Edit the constants at the top of `aerent_mint.py`. Read each value from `GET /api/v2/drops/{slug}` and verify it on-chain:

```python
SLUG, NFT, STAGE_LABEL, STAGE_START, STAGE_END
```

## Run

```bash
python3 aerent_mint.py                       # dry run: full flow, never broadcasts
nohup python3 -u aerent_mint.py --arm > aerent.log 2>&1 &   # armed; sleeps until the stage opens
tail -f aerent.log
```

`run_testdrop.py` runs the unmodified bot against your own test drop. Only the target constants are
overridden, which makes it a real end-to-end mainnet rehearsal.

## Tests

```bash
./run_tests.sh      # needs Foundry `anvil`
```

* **Unit (20):** validator edge cases, plus real OpenSea calldata fixtures.
* **Fork:** a local anvil fork of mainnet, pinned to a block before the AERENT sale. A test signer is registered on the real AERENT contract, and the bot mints with a genuine EIP-712 signature.
* **Scenarios:**
  * late stage flip (409 / Team-Mint 422)
  * invalid signature (1 tx, then stop)
  * public/paid calldata (0 tx)
  * wallet not on the list (0 tx)

Fork tests use a **throwaway wallet funded on the fork** and a mocked API. Nothing reaches mainnet.

## Files

| File | Purpose |
|---|---|
| `aerent_mint.py` | Bot v2 (current) |
| `thousand_mint.py` | Bot v1 (Thousand WL mint) |
| `run_testdrop.py` | Live rehearsal against your own drop |
| `eip712.py`, `fork_helpers.py`, `fork.sh` | Test tooling |
| `test_*.py`, `fixture_*` | Tests and real OpenSea calldata fixtures |

## Security

* No secrets in the repository. Keys are read from files outside it (or from env vars), and the logger masks them.
* `.gitignore` blocks `*.env`, keys, logs and OpenSea session files.
* The bot only ever sends `value = 0` to SeaDrop.
* Fork tooling tracks its own process by PID file and never uses `pkill anvil` (that name is shared by other daemons, e.g. Dovecot).

## License

MIT
