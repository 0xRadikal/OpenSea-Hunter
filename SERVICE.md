# OpenSea-Hunter: Telegram subscription service

A Telegram bot. Users subscribe (Telegram Stars), register a burner wallet and their own OpenSea API key,
then send a collection link. The bot checks everything and schedules the mint. At T0 it runs the **unchanged,
mainnet-proven** engine `aerent_mint.py` for that user.

## Flow (user)
1. `💳 اشتراک`: pay with Stars (`XTR`, `provider_token=""`). Payments are idempotent by `telegram_payment_charge_id`.
2. `👛 ولت‌ها`: send the burner private key. The message is deleted immediately, the address is derived, and the key is stored encrypted (AES-256-GCM).
3. `🔑 کلید API`: a **personal** OpenSea key per user:
   * `⚡ ساخت خودکار کلید API` opens `keypage/` as a Telegram Mini App. The user's own browser calls
     `POST https://api.opensea.io/api/v2/auth/keys` (CORS verified) and returns the key through `WebApp.sendData`.
   * or the user pastes a key / the JSON from `curl -X POST .../auth/keys`.
   * Every key is tested against OpenSea before it is stored.
4. `🎯 تسک جدید`: the user sends a link or slug. The bot then:
   * resolves `/collection/<slug>` and `/item/robinhood/<contract>/<id>`
   * fetches the drop and checks the chain, `seadrop_v1_erc721`, sold-out state, contract code, and that a SeaDrop signer is registered
   * shows all stages; only **free `signed_presale`** stages can be selected
   * checks gas balance and current holdings, then **signs in to OpenSea with the wallet (SIWE)**. It creates a 1-day
     `read:eligibility` PAT, exchanges it for a JWT, revokes the PAT, and calls `/drops/{slug}/eligibility`.
     A wallet that is not on the allowlist is refused before any task exists.
   * refuses the task if the API key expires before the stage starts
5. Shortly before T0 (`SPAWN_BEFORE_S`) the runner starts `service/engine_wrapper.py`, which runs the engine with
   `--arm`. The user is notified of the result (minted / failed + last log lines).

## Security model
* Master key: `MASTER_KEY_FILE` (32 random bytes, chmod 600, outside repo and DB). Create it once:
  `python -c "from service.vault import create_master_key as c; c('/root/osh-master.key')"`.
  **Back it up.** Losing it makes every stored wallet undecryptable, and the code refuses to overwrite it.
* The AAD binds each ciphertext to (user id, address/purpose), so copying blobs between rows fails.
* Secrets reach the engine **only through a stdin pipe** (never argv, env or disk). The engine masks them in logs,
  and the tests assert that no secret appears in logs, bot messages or any file under `DATA_DIR`.
* The engine refuses anything other than `mintSigned`, price 0, value 0, quantity 1, correct NFT and recipient.
  `TARGET_HELD` is set to current holdings + 1.
* httpx INFO logging is disabled (it would log the URL containing the bot token).
* Private chats only. Admin commands (`/grant <uid> <days>`, `/ban <uid> [1|0]`, `/stats`) are restricted to `ADMIN_IDS`.

### Honest limits (verified)
* The server holds the keys (custodial). That is required for unattended speed. Users must use burner wallets.
* OpenSea instant keys: **2 per day per IP**, valid for 7 days. That is why they are created from the user's IP and not the server's.
* Scope is exactly the engine's proven scope: Robinhood Chain, SeaDrop V1 ERC-721, free signed stages.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-service.txt
cp deploy/osh-bot.env.example /root/osh-bot.env && chmod 600 /root/osh-bot.env   # fill in
set -a; . /root/osh-bot.env; set +a; .venv/bin/python -m service.bot
# production: deploy/opensea-hunter-bot.service (KillMode=process so in-flight mints survive restarts)
```

## Key page hosting
Serve `keypage/index.html` over HTTPS (see `deploy/nginx-keypage.conf`) and set `KEYPAGE_URL` to that URL.
The menu button «⚡ ساخت خودکار کلید API» appears only when `KEYPAGE_URL` is set. `sendData` works only from a
reply-keyboard WebApp button, which is what the bot uses.

## Tests
```bash
.venv/bin/python -m pytest -q tests -m "not fork"   # offline
PATH=$HOME/.foundry/bin:$PATH .venv/bin/python -m pytest -q tests   # + anvil fork, fake OpenSea, fake Telegram
./run_tests.sh                                      # original engine suite
```
`tests/test_bot_e2e.py` drives the real bot code through a fake Bot API server. It covers payment (including a tampered
amount and duplicate delivery), wallet and key registration, link → stage → SIWE eligibility → task, and an automatic
mint on a fork of the real AERENT contract. It also checks that a non-eligible wallet is refused, that group chats are
ignored, that admin commands are gated, and that no secret leaks.
