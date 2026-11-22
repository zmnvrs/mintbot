# MintBot — Telegram mint referral bot (in-Telegram only)

An admin-style Telegram bot, exactly like the admin's bot: users
connect their Binance agent wallet by scanning a QR code **inside
Telegram**, then mint Agent ID (AI) shares and claim a **$3** reward —
all in the bot, no web page, nothing outside Telegram.

**How it works:** each user pairs their own agent wallet with the
bot (Binance sign-in QR, scanned in the Binance app). The bot runs
the mint for them through the backend (the `baw` CLI) from their own
paired wallet. The referral on every mint goes to the owner's W1
wallet. The bot never holds a private key — `baw` handles auth; the
bot only keeps a QR code ID and session state.

## Buttons (like the admin's bot)

- **Connect Wallet** — the bot sends a Binance sign-in QR as an
  image. The user scans it in the Binance app to pair their agent
  wallet. The QR expires in ~5 minutes; on expiry the bot says
  "QR-এর মেয়াদ শেষ হয়ে গেছে। আবার Connect Wallet চাপুন।" (press
  Connect Wallet again for a new one).
- **Check status** — shows the paired wallet address, BNB balance,
  Agent ID status and mint status. Press it after scanning the QR to
  finish pairing.
- **Mint Ten Share** / **Mint Five Share** — mints Agent ID (AI)
  shares from the user's paired wallet. The bot first shows the live
  numbers (cost, expected AI, expected sell value) and the user taps
  **Confirm Mint**. The mint only runs on that explicit tap, only
  with a clean `baw` preview (value = live cost exactly, no extra
  approvals). After the mint the bot immediately sells the AI just
  received and reports what came back.
- **Claim $3** — after the user's first mint through the bot, one
  $3 USDT claim per account, paid from the owner's W1 wallet.
- **Help** — explains the flow. **Disconnect** — unpairs the wallet.
- **Join Channel**, **Check Balance** (send a 0x address, the bot
  reads the BNB balance and says Eligible / Not eligible), **Tasks**
  (joining the channel is optional and never blocks minting).

## The mint (what actually happens)

The mint contract is `0xa4bFa31f615F8e2a13dce3d93C0996CC5a56763D`
(BNB Smart Chain, chainId 56). The referrer on every mint is W1
(`0xE012133823df5452de9c10a73fE935d26B995A97`) — never anyone else,
and the minting wallet can never be W1 itself (self-referral is not
allowed). Per mint: `mint(qty, W1, maxCost, totalMinted+1, 0,
now+600)` with `value = maxCost` (live `costOf(qty)` exactly). If
`costOf(10)` is above 0.08 BNB (0.008 BNB/share cap) the bot stops.

**The mint goes through the Telegram Web App mini app
(`mint_app.html`, served at `/mint-app`):** the user connects their
wallet there via WalletConnect and sends the mint transaction
DIRECTLY on-chain (2–3 seconds). The `baw` preview path is NOT
used for the mint, because the baw preview simulation cannot
simulate this mint — it fails with 351805 for every parameter,
while a direct on-chain call succeeds. The mini app reports the
mint back to the bot (Telegram WebApp `sendData` with the tx hash),
the bot records it and immediately sells the AI via `baw` (the
sell is a simple approve+swap, which the preview handles fine).
When `MINI_APP_URL` is not set, the Mint buttons fall back to the
old `baw` quote/confirm flow (kept so the bot still works before
the mini app has a public URL).

**Slots:** the slots shown are the OWNER'S (W1) REFERRAL slots, not
the global mint supply. The referral quota is `quotaOf(referrerIdOf(W1))`
(total slots for W1's referral), `mintedCount(...)` is how many shares
were already minted under it, and slots left = quota − minted (live
2026-11-22: 2199 total, 10 minted, 2189 left). The quote (in the bot
and in the mini app) shows them as "🍬 Mint slots baki: 2189/2199"
alongside the W1 referral state (level, valid invites, owed bonus).
If the referral slots left are fewer than the requested shares, the
mint is blocked with a plain message
("Referral-e ar kono slot nei — mint kora jabe na") so nobody
loses gas on a sold-out referral. The global mint supply
(`TOTAL_SHARES()` = 4,440,000) is only a secondary reference.

**Hosting the mini app:** Telegram Web Apps need a public HTTPS
URL. Deploy this repo to Render (the `render.yaml` web service
serves the Flask app, including `/mint-app` and `/api/quote`),
then set `MINI_APP_URL` in the bot's `.env` to that public URL
(e.g. `https://mintbot-xxxx.onrender.com`) and restart the bot.
The Render service only serves the page and the quote API — the
bot itself keeps running on the owner's machine in polling mode,
where the per-user `baw` sessions and the W1 session live (claims
and the instant sell run there). Set `WALLETCONNECT_PROJECT_ID`
(from https://cloud.walletconnect.com) in the Render dashboard
and in the bot's `.env`; it is injected into the page at runtime
and is never committed.

Before minting the bot checks: the wallet is paired, it holds a .ai
Agent ID (the contract reverts without one), it has enough BNB for
the cost + gas, and Developer Mode is ON
in the Binance app (Agentic Wallet → Settings → Developer Mode —
`baw` contract calls need it). Any failed check stops the mint and
the bot says why, plainly. Nothing is ever sent without the user
confirming in their own wallet.

**Live economics (verified on-chain, 2026-11-22):** a 10-share mint
costs 0.078 BNB and the minter receives ~11.38B AI (the BNB paid
converts to AI at the pool price, plus 50M AI per share). Selling
that AI back gives ~0.0769 BNB — the mint+sell loses ~0.001 BNB
(~$0.60) plus gas. **The sell is NOT a profit and NOT a "reward"**
— it gets the user's BNB back. The bot always shows the live cost
vs sell value before the user confirms, using live `costOf`, pair
reserves and the router's `getAmountsOut` — never a guessed number.

**Referral:** W1 earns 5% at base tier (~$2.30 per 10-share mint),
10% silver at 10 invites (~$4.60), 15% gold at 100 invites
(~$6.87). Rewards accrue as `owedOf(W1)` and need `claimOwed()` —
they are not sent automatically.

## The $3 claim — read this first

- One claim per Telegram account, only after that account's first
  successful mint through the bot.
- Paid in USDT from W1 (`0xE012...`), via a USDT contract-call
  transfer from the W1 `baw` session (not `wallet send` — that is
  blocked unless the recipient is in W1's address book).
- **W1 must hold the USDT.** W1 currently holds ~0 USDT, so claims
  CANNOT be paid until the owner funds W1. If the pool is short, the
  bot tells the user plainly that the reward pool is empty and to
  contact the owner — it never fails silently and never sends a
  partial payment.
- **The claim loses money at base tier:** paying $3 against ~$2.30
  referral per 10-share mint loses ~$0.70 per user, until W1 reaches
  silver (10%, ~$4.60) or gold (15%, ~$6.87). Fund W1 accordingly.
- **Deployment caveat:** the W1 `baw` session lives on the owner's
  own machine (`/home/hatch`). On Render it does not exist, and the
  free tier's filesystem is ephemeral — so claims cannot be paid
  from a Render deploy unless the W1 session is present on that
  host. If W1 is not connected there, the bot reports the reward
  pool as unavailable. Minting (which uses each user's own paired
  session) works wherever the bot runs.

## Architecture — per-user baw sessions

- `baw_users.py` — everything that touches `baw` or the chain:
  per-user session dirs (`~/.baw-users/<telegram_user_id>/`),
  sign-in QR generation, verify, status/address/Agent ID/settings
  reads, live chain reads (`costOf`, `totalMinted`, `paidOnce`,
  pair reserves, router quotes), mint preview/execute, instant
  sell, and the W1 claim payment. `baw` is invoked as a subprocess
  with that user's HOME — sessions can never leak between users.
- `bot.py` — the Telegram bot (Flask webhook + plain Bot API calls,
  polling mode with `--poll`). All seven buttons, the quote →
  confirm mint flow, the claim flow, balance check, tasks.
- Per-user bot records (paired address, Agent ID, mint/claim state,
  pending QR) live in a small JSON state file
  (`<BAW_USERS_HOME>/state.json`). A free Render service sleeps and
  restarts, and its filesystem is ephemeral — records and `baw`
  sessions are wiped then, and users have to pair again. By design.
- The bot moves funds ONLY in two flows, both only on the owning
  user's explicit button tap: (1) the mint (user's paired wallet →
  mint contract, value = live cost), (2) the $3 claim (W1 → the
  claiming user's wallet, USDT, once per account after a mint).
  Nothing else is ever moved, no unknown contract is ever approved,
  and W1 is never minted from through this bot.

## The baw CLI

The backend is the Binance Agentic Wallet CLI (`baw`). It is wiped
when the VM restarts — reinstall it with:

```bash
npm install -g @binance/agentic-wallet@1.12.0
```

The Render `buildCommand` in `render.yaml` does this install on
every deploy. If `baw` is missing on the host, pairing/mint/claim
fail with a clear "baw CLI is not installed" message instead of
anything half-done.

Useful `baw` facts (from the live CLI, v1.12.0):
- `baw auth signin --json` → `qrCodeId`, `expireAt` (ms epoch),
  `urlForWeb` (the QR encodes this), `pairingCode` (~5 min validity).
- `baw auth verify --qrCodeId <id>` after the scan (polls until
  wallet creation completes); `baw wallet status --json` confirms.
- `baw wallet agent-id --json` shows the claimed `.ai` name;
  `baw wallet settings --json` shows `devMode.enabled`.
- `baw contract-call preview --binanceChainId 56 --from <addr>
  --to <contract> --value <wei> --inputData <hex> [--gasLimit N]
  --json`, then `baw contract-call execute --requestId <id> --json`.
  Preview requestIds expire in ~2–5 min. `baw` gas estimation can
  under-estimate — the bot retries a failed preview once with an
  explicit `--gasLimit 400000`.

## Deploy step by step

### 1. Create the bot (2 minutes, you do this)

1. Open Telegram and talk to **@BotFather**.
2. Send `/newbot`, pick a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456:ABC...` — copy it.
   This is the `BOT_TOKEN`. Never share it or commit it to git.

### 2. Put the code on GitHub

The repo is `zmnvrs/mintbot` (public, branch `main`). Push the code
after changes (the push uses the GitHub connector's snapshot push):

```bash
git add bot.py baw_users.py requirements.txt render.yaml Procfile .env.example .gitignore README.md
git commit -m "Mint referral bot"
git push origin main
```

### 3. Create the free Render service

1. Go to https://dashboard.render.com → **New +** → **Web Service**.
2. Connect your GitHub repo (`zmnvrs/mintbot`).
3. Settings: **Runtime** Python, **Build Command**
   `pip install -r requirements.txt && npm install -g
   @binance/agentic-wallet@1.12.0`, **Start Command**
   `gunicorn bot:app --bind 0.0.0.0:$PORT`, **Plan** Free.
   (If you use the `render.yaml` blueprint instead, these are filled
   in automatically — see step 4 for the env vars either way.)
4. Add these **Environment Variables** in the Render dashboard:
   | Variable | Example |
   |---|---|
   | `BOT_TOKEN` | token from @BotFather |
   | `CLAIM_USD` | `3` |
   | `MIN_BNB_USD` | `41` |
   | `TASK_CHANNELS` | `@channel1,@channel2` or a numeric chat ID like `-1001234567890` (or leave empty). An invite link does NOT work here — to get the numeric ID, forward any message from the channel to @getmyid_bot. |
   | `CHANNEL_INVITE_URL` | `https://t.me/+s2WrevCJwkFkOTVl` (Join Channel button) |
   | `OWNER_CONTACT` | `@imzaman7` (shown in the welcome message) |
   | `REWARD_TEXT` | `$3 per account` |
   | `WEBHOOK_URL` | `https://YOURAPP.onrender.com` (your Render URL) |
   | `BAW_USERS_HOME` | (leave unset — defaults to `~/.baw-users`) |
   | `W1_BAW_HOME` | (leave unset unless W1's session lives elsewhere) |
5. Deploy. When it is live, note your app URL, e.g.
   `https://mintbot-xxxx.onrender.com`.

### 4. Point Telegram at the bot (set the webhook)

After the first deploy, open this URL in your browser (replace with
your real values):
```
https://YOURAPP.onrender.com/setwebhook
```
It uses the `WEBHOOK_URL` env var. Or do the same by hand:
```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://YOURAPP.onrender.com/webhook
```
You should get `{"ok":true,...}` back.

### 5. Test it

Open your bot in Telegram and send `/start`. You should see the
welcome message with **Connect Wallet**, **Check status**,
**Mint Ten Share**, **Mint Five Share**, **Claim $3**, **Help**,
**Disconnect**. Press Connect Wallet — a Binance QR should appear.
Scan it in the Binance app, press Check status, and it should show
your wallet as connected. (Do not mint with a real wallet until you
have read the mint economics above.)

### 6. Fund W1 for claims

Before anyone can claim, send USDT to W1
(`0xE012133823df5452de9c10a73fE935d26B995A97`) — at least $3 per
expected claim, plus a little BNB for the claim's gas. Remember the
claim loses ~$0.70 per user at base tier (see above).

### 7. Tasks — add the bot as admin of each channel

`getChatMember` only works when the bot is an **admin** of the channel.
Add your bot as an admin of every channel listed in `TASK_CHANNELS`,
or the bot will report "could not verify" for that channel.

## Free Render notes

- A free web service **sleeps after ~15 minutes** with no traffic.
  A Telegram webhook wakes it up — the first message after a sleep
  can take ~30–60 seconds to answer. That is normal on the free plan.
- Restarts wipe the in-memory user state AND the per-user `baw`
  sessions and the state file (ephemeral filesystem). Users have to
  pair again after a restart — tell them that in Help if it
  confuses people.
- If the claim amount changes, update `CLAIM_USD` in the Render
  dashboard and restart the service (Manual Deploy → Restart).

## Local test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install -g @binance/agentic-wallet@1.12.0
BOT_TOKEN=test-token python bot.py
# in another terminal:
curl http://127.0.0.1:5000/health
python test_bot.py   # mocked baw + chain reads, no real wallet
```

## Files

- `bot.py` — the whole bot (Flask webhook + plain Telegram Bot API
  calls, polling mode with `--poll`)
- `baw_users.py` — per-user `baw` sessions, chain reads, mint/sell/
  claim logic
- `requirements.txt` — Flask, requests, gunicorn, qrcode[pil],
  eth-abi, eth-hash[pycryptodome]
- `render.yaml` — Render blueprint (free web service)
- `Procfile` — `web: gunicorn bot:app`
- `.env.example` — all env vars
- `test_bot.py` — offline tests (mocked `baw`, no real token, no
  real wallet, no network calls to Telegram)
