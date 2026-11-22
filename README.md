# MintBot — Telegram mint referral bot

An admin-style Telegram bot: users press **Start**, the bot checks they
hold enough BNB, asks them to complete tasks (join channels), and gives
them **Mint 5** and **Mint 10** buttons that open the bot's **own mint
page** inside Telegram as a Web App (Mini App) — the user connects
their own wallet there and mints, wallet connect + instant mint all
inside Telegram, just like the admin's bot. No external mint site is
needed: the page calls the Agent ID (AI) mint contract on BNB Smart
Chain directly.

**How it works:** the bot is only the funnel. It never connects a wallet
and never mints anything itself. The Mint buttons open the bot's own
mint page inside Telegram as a Mini App — each user connects **their
own** wallet there (WalletConnect) and mints on the site, without
leaving Telegram. (The Binance agent wallet API cannot be connected
to a public bot — that would be a drain risk.)

## What the bot does

- `/start` — welcome message with the reward text and 4 buttons:
  **Mint 5**, **Mint 10** (open the bot's own mint page inside Telegram
  as a Mini App), **Check Balance**, **Tasks**.
- **Check Balance** — the user sends a BSC wallet address (0x...). The
  bot reads the BNB balance from a public BSC RPC and the BNB price from
  the Binance public ticker (no API key), then says Eligible / Not
  eligible with the real USD value. Minimum is `MIN_BNB_USD` (default $41).
- **Tasks** — joining the channel is **optional** and never blocks
  minting. If channels are listed in `TASK_CHANNELS`, the bot checks
  with `getChatMember` whether the user joined each one and reports
  done / pending. If none are listed it says there are no required
  tasks and the user can mint right away.
- User state is kept **in memory only**. A free Render service sleeps and
  restarts, and that wipes it — by design, nothing is stored.

## The mint page (own page, no external site)

The Mint buttons open `{WEBHOOK_URL}/mint?qty=5` (or `qty=10`) — a
page served by this app. It needs a **public HTTPS URL**, so it only
works once the app is deployed (Render); in polling mode with no
public URL the buttons fall back to the `MINT_URL` links instead.

The page (`mint_page.py`, served at `GET /mint`):
1. Connects the user's own EVM wallet inside Telegram via
   **WalletConnect** — needs a `WC_PROJECT_ID` (free from
   https://cloud.walletconnect.com — create a project, copy the
   Project ID). Without it the page says wallet connect is not
   configured yet and cannot connect.
2. After connect it reads the **live** numbers from the Agent ID mint
   contract (`0xa4bFa31f615F8e2a13dce3d93C0996CC5a56763D`, BNB Chain):
   `costOf(qty)`, `totalMinted()`, `paidOnce(wallet)`, and the
   AI/WBNB pair reserves — and shows the user, **before** they
   confirm: shares, exact mint cost in BNB, the AI they will receive
   (estimated from the live pool price), and the expected sell value
   from the router's `getAmountsOut`. If the expected sell value is
   below the mint cost it warns plainly that selling now would lose
   money. The Mint button stays disabled until these numbers load.
3. On confirm it re-reads `costOf`/`totalMinted` fresh, simulates the
   mint (static call — if it would revert, e.g. no `.ai` Agent ID, it
   stops and shows the error), then sends
   `mint(qty, W1, maxCost, totalMinted+1, 0, now+600)` with
   `value = maxCost`. The referrer is always W1
   (`0xE012133823df5452de9c10a73fE935d26B995A97`); the minting wallet
   can never be W1 itself. If the cost is above the cap (0.008 BNB
   per share) the page stops. It shows the tx hash + BscScan link.
4. Instant sell is **OFF by default** (`ENABLE_INSTANT_SELL=0`). When
   on, after a mint it sells ONLY the AI actually received (wallet
   balance change), approves the router for exactly that amount, and
   swaps with a 5% slippage guard. **Live economics (2026-11-22): a
   10-share mint costs 0.078 BNB and the AI received sells back for
   ~0.077 BNB — the sell loses ~$0.60 plus gas. It is NOT a profit
   and NOT a "reward".** Keep instant sell off unless you have
   verified the live numbers yourself.

Safety: BNB is never sent directly to the contract address (only as
the mint call's value); no token is approved except the single AI
approval for the sell; no other contract is called; any failed read
or simulation stops the page and shows the error — values are never
guessed.

## Mint links — fallback only (used when WEBHOOK_URL is not set)

**Way 1 — one template link with a `{qty}` placeholder:**
`MINT_URL=https://example.com/mint?ref=YOURCODE&qty={qty}`
The Mint 5 button opens the link with `qty=5`, Mint 10 with `qty=10`.

**Way 2 — a separate link for each button:**
Set both `MINT_URL_5` and `MINT_URL_10`. When both are set they are
used instead of `MINT_URL`. (If only one is set, the bot falls back to
the `MINT_URL` template.)

## Deploy step by step

### 1. Create the bot (2 minutes, you do this)

1. Open Telegram and talk to **@BotFather**.
2. Send `/newbot`, pick a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456:ABC...` — copy it.
   This is the `BOT_TOKEN`. Never share it or commit it to git.

### 2. Get a WalletConnect project ID (for the mint page)

1. Go to https://cloud.walletconnect.com, sign up free, create a
   project.
2. Copy its **Project ID** — this is the `WC_PROJECT_ID` env var.
   (It is a public identifier, not a secret, but keep it in env vars
   anyway — never hardcode it.)
3. Without it the mint page shows "wallet connect is not configured
   yet" and no wallet can connect.

The old agentidai.meme site is dead (NXDOMAIN) and was Safe Browsing
phishing-flagged — the bot no longer depends on it. The `MINT_URL`
links are only a fallback for when the app has no public URL.

### 3. Put the code on GitHub

1. Create a **new empty repo** on github.com (in the browser — the
   GitHub app cannot create repos).
2. From this folder, push the code:
   ```bash
   git init
   git add bot.py mint_page.py requirements.txt render.yaml Procfile .env.example .gitignore README.md
   git commit -m "Mint referral bot"
   git branch -M main
   git remote add origin https://github.com/YOURNAME/YOURREPO.git
   git push -u origin main
   ```

### 4. Create the free Render service

1. Go to https://dashboard.render.com → **New +** → **Web Service**.
2. Connect your GitHub repo.
3. Settings: **Runtime** Python, **Build Command**
   `pip install -r requirements.txt`, **Start Command**
   `gunicorn bot:app --bind 0.0.0.0:$PORT`, **Plan** Free.
   (If you use the `render.yaml` blueprint instead, these are filled in
   automatically — see step 5 for the env vars either way.)
4. Add these **Environment Variables** in the Render dashboard:
   | Variable | Example |
   |---|---|
   | `BOT_TOKEN` | token from @BotFather |
   | `MINT_URL` | fallback mint link, only when `WEBHOOK_URL` is not set (see above) |
   | `MINT_URL_5` | (only if using the fallback way 2) |
   | `MINT_URL_10` | (only if using the fallback way 2) |
   | `WC_PROJECT_ID` | WalletConnect Cloud project ID (mint page wallet connect) |
   | `ENABLE_INSTANT_SELL` | `0` (default off — see the mint page section) |
   | `MIN_BNB_USD` | `41` |
   | `TASK_CHANNELS` | `@channel1,@channel2` or a numeric chat ID like `-1001234567890` (or leave empty). An invite link does NOT work here — to get the numeric ID, forward any message from the channel to @getmyid_bot. |
   | `CHANNEL_INVITE_URL` | `https://t.me/+s2WrevCJwkFkOTVl` (Join Channel button) |
   | `OWNER_CONTACT` | `@imzaman7` (shown in the welcome message) |
   | `REWARD_TEXT` | `$3 per account` |
   | `WEBHOOK_URL` | `https://YOURAPP.onrender.com` (your Render URL) |
5. Deploy. When it is live, note your app URL, e.g.
   `https://mintbot-xxxx.onrender.com`.

### 5. Point Telegram at the bot (set the webhook)

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

### 6. Test it

Open your bot in Telegram and send `/start`. You should see the
welcome message with **Mint 5**, **Mint 10**, **Check Balance** and
**Tasks** buttons. Try Check Balance with a wallet address.

### 7. Tasks — add the bot as admin of each channel

`getChatMember` only works when the bot is an **admin** of the channel.
Add your bot as an admin of every channel listed in `TASK_CHANNELS`,
or the bot will report "could not verify" for that channel.

## Free Render notes

- A free web service **sleeps after ~15 minutes** with no traffic.
  A Telegram webhook wakes it up — the first message after a sleep
  can take ~30–60 seconds to answer. That is normal on the free plan.
- Restarts wipe the in-memory user state (a user mid balance-check has
  to press Check Balance again). Nothing else is lost.
- If the mint link changes, update the env vars in the Render
  dashboard and restart the service (Manual Deploy → Restart).

## Local test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BOT_TOKEN=test-token MINT_URL='https://example.com/mint?ref=X&qty={qty}' \
  python bot.py
# in another terminal:
curl http://127.0.0.1:5000/health
python test_bot.py   # simulated /start + balance + tasks, no real token
```

## Files

- `bot.py` — the whole bot (Flask webhook + plain Telegram Bot API calls)
- `mint_page.py` — the bot's own mint Web App page (served at GET /mint)
- `requirements.txt` — Flask, requests, gunicorn
- `render.yaml` — Render blueprint (free web service)
- `Procfile` — `web: gunicorn bot:app`
- `.env.example` — all env vars, with the two mint-link ways documented
- `test_bot.py` — offline tests (no real token, no network calls to Telegram)
