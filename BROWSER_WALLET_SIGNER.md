# Browser-wallet signer (no private key in Python)

`browser_mint.py` is an opt-in signer path for the current AERENT/OpenSea-Hunter flow.

## Security boundary

The Python process receives only:

- the public minter address
- an OpenSea API key
- OpenSea's signed mint calldata

It does not receive, derive, serialize, or store a wallet private key.

The final transaction is submitted with the browser wallet's EIP-1193 `eth_sendTransaction`. The wallet keeps its signing key internally.

The local callback server binds to `127.0.0.1` and uses a random one-run bearer token.

## Important limitation

This removes the private key from the bot, not the cryptographic authorization requirement.

The wallet must still approve the transaction. Therefore this mode is not an unattended/headless mint bot. It trades key exposure for a wallet confirmation step.

A fully unattended signer always has some authorization mechanism somewhere: a private key, hardware signer, HSM/KMS, managed wallet, session key, or smart-account signer.

## Usage

Set the OpenSea API key without putting a wallet private key in the environment:

    export OPENSEA_API_KEYS="..."
    python3 browser_mint.py --wallet 0xYourEligibleWallet

The program waits for the configured signed/free stage, validates OpenSea calldata, then prints a localhost URL.

Open that URL on the same machine in a browser with an EVM wallet, connect the exact same wallet address, and approve the transaction.

For a non-broadcast validation run:

    python3 browser_mint.py --wallet 0xYourEligibleWallet --prepare-only

## What is intentionally unchanged

The existing `aerent_mint.py` production path is not modified. Its private-key signer remains available.

This browser path reuses its:

- OpenSea API call
- calldata validation
- stage timing checks
- Robinhood Chain RPC checks
- signed-window validation
- receipt verification

## Performance trade-off

The original bot can sign and broadcast immediately from Python. Browser-wallet mode cannot make that same guarantee because the wallet confirmation/UI is outside the Python process.

For a truly unattended deployment without putting the key in the Python process, use an external signer/HSM/KMS or a managed smart-account/session-key design instead.