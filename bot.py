"""Mint referral bot — admin-style, in-Telegram only.

Users press /start, connect their Binance agent wallet by scanning a
QR code (the baw pairing QR) right inside Telegram, then mint Agent ID
(AI) shares from their paired wallet through the backend — no web
page and nothing outside Telegram. After their first mint they can
claim a $3 reward (one per account), paid in USDT from the owner's W1
wallet.

How it works: every Telegram user gets an ISOLATED baw session (a
separate HOME dir per Telegram user id, see baw_users.py). The bot
never holds a private key — baw handles auth; the bot only keeps a
qrCodeId and session state. Minting happens only on the owning user's
explicit button tap, only from their own paired wallet, only with a
clean baw preview (value = live mint cost exactly, no extra
approvals). After a mint the bot immediately sells the AI just minted
(instant reward) and reports the proceeds.

Buttons (like the admin's bot): Connect Wallet, Check status,
Mint Ten Share, Mint Five Share, Claim $3, Help, Disconnect — plus
Join Channel, Check Balance, Tasks.

Webhook mode (for a free Render web service): Telegram sends updates
to POST /webhook. Polling mode (--poll) works without a public URL.

Config is env vars only — never hardcode a token, address or ID:
    BOT_TOKEN       Telegram bot token from @BotFather
    CLAIM_USD       Reward paid per account after the first mint
                    (default 3). Paid in USDT from W1.
    MIN_BNB_USD     Minimum BNB value in USD to be eligible (default 41)
    TASK_CHANNELS   Comma-separated @handles or numeric chat IDs users
                    must join (optional). An invite link does NOT work
                    here — getChatMember needs the channel's @handle or
                    its numeric ID (e.g. -1001234567890). To get the
                    numeric ID, forward any message from the channel to
                    @getmyid_bot.
    CHANNEL_INVITE_URL
                    Invite link for the channel (e.g.
                    https://t.me/+xxxx) — shown as a "Join Channel"
                    button and used as the join link in Tasks.
    OWNER_CONTACT   Shown in the welcome message as the owner's contact
                    (e.g. @imzaman7).
    REWARD_TEXT     Reward text shown in the welcome message
                    (default "$3 per account")
    WEBHOOK_URL     Public URL of this app, e.g.
                    https://mintbot-xxxx.onrender.com (for /setwebhook)
    BSC_RPC         BSC JSON-RPC endpoint
                    (default https://bsc-dataseed.bnbchain.org)
    BAW_USERS_HOME  Root dir for per-user baw sessions
                    (default ~/.baw-users)
    W1_BAW_HOME     The owner's W1 baw session HOME — claims only,
                    never mints (default /home/hatch)
    BAW_STATE_FILE  Where per-user bot records live
                    (default <BAW_USERS_HOME>/state.json)
    BAW_BIN         baw binary path (default: whatever is on PATH)

Per-user bot records (paired address, Agent ID, mint/claim state,
pending QR) live in the state file. A free Render service sleeps and
restarts, and its filesystem is ephemeral — records and baw sessions
are wiped then, and users have to pair again. By design.
"""

import json
import os
import re
import sys
import time

import requests
from flask import Flask, request, jsonify

import baw_users
from baw_users import BawError

app = Flask(__name__)


# ---------------------------------------------------------------- config

def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


CLAIM_USD = _env_float("CLAIM_USD", 3)
MIN_BNB_USD = _env_float("MIN_BNB_USD", 41)
TASK_CHANNELS = [c.strip() for c in os.environ.get("TASK_CHANNELS", "").split(",") if c.strip()]
CHANNEL_INVITE_URL = os.environ.get("CHANNEL_INVITE_URL", "").strip()
OWNER_CONTACT = os.environ.get("OWNER_CONTACT", "").strip()
REWARD_TEXT = os.environ.get("REWARD_TEXT", "$3 per account").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
BSC_RPC = os.environ.get("BSC_RPC", "https://bsc-dataseed.bnbchain.org").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

TELEGRAM_API = "https://api.telegram.org/bot{}".format(BOT_TOKEN)
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BSCSCAN_TX = "https://bscscan.com/tx/{}"

# chat_id -> {"step": "awaiting_address"} — memory only, wiped on restart.
user_state = {}

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
QR_VALID_MS = 5 * 60 * 1000  # baw QR codes live ~5 minutes


# ------------------------------------------------------- telegram helpers

def tg(method, payload):
    """Call the Telegram Bot API. Returns the parsed JSON, or None on error."""
    try:
        resp = requests.post("{}/{}".format(TELEGRAM_API, method),
                             json=payload, timeout=15)
        return resp.json()
    except Exception as exc:  # network error, bad token, non-JSON reply, ...
        app.logger.warning("telegram api %s failed: %s", method, exc)
        return None


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)


def send_photo(chat_id, photo_bytes, caption=None, reply_markup=None):
    """Send a PNG (e.g. the Binance pairing QR) as a Telegram photo."""
    files = {"photo": ("qr.png", photo_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post("{}/sendPhoto".format(TELEGRAM_API),
                             data=data, files=files, timeout=30)
        return resp.json()
    except Exception as exc:
        app.logger.warning("telegram sendPhoto failed: %s", exc)
        return None


def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload)


def _bold(text):
    """Render text in Unicode mathematical bold.

    Telegram inline-keyboard buttons have no bold formatting, so the
    button labels use bold-looking Unicode characters instead.
    """
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - ord("a")))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7CE + ord(ch) - ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def main_keyboard():
    """Inline keyboard — the admin bot's seven buttons, then the rest."""
    rows = [
        [{"text": _bold("Connect Wallet"), "callback_data": "connect_wallet"},
         {"text": _bold("Check status"), "callback_data": "check_status"}],
        [{"text": _bold("Mint Ten Share"), "callback_data": "mint_10"},
         {"text": _bold("Mint Five Share"), "callback_data": "mint_5"}],
        [{"text": _bold("🧧 Claim $3"), "callback_data": "claim_3"}],
        [{"text": _bold("Help"), "callback_data": "help"},
         {"text": _bold("Disconnect"), "callback_data": "disconnect"}],
    ]
    if CHANNEL_INVITE_URL:
        rows.append([{"text": _bold("📢 Join Channel"), "url": CHANNEL_INVITE_URL}])
    rows.append([
        {"text": _bold("Check Balance"), "callback_data": "check_balance"},
        {"text": _bold("Tasks"), "callback_data": "tasks"},
    ])
    return {"inline_keyboard": rows}


def welcome_text():
    text = (
        "Welcome!\n\n"
        "Earn {} per account.\n\n"
        "How it works:\n"
        "1. <b>Connect Wallet</b> — scan the Binance QR in the app to "
        "pair your agent wallet.\n"
        "2. <b>Mint Five Share</b> or <b>Mint Ten Share</b> — mint from "
        "your paired wallet. The referral goes to the owner.\n"
        "3. <b>Claim $3</b> — after your first mint through the bot, "
        "one claim per account.\n\n"
        "You need a .ai Agent ID in your wallet and enough BNB for the "
        "mint cost + gas. Turn on Developer Mode in your Agentic "
        "Wallet settings first."
    ).format(REWARD_TEXT)
    if OWNER_CONTACT:
        text += "\n\nOwner: {}".format(OWNER_CONTACT)
    return text


def help_text():
    return (
        "How to use this bot:\n\n"
        "1. <b>Connect Wallet</b> — I send a Binance sign-in QR. Open "
        "the Binance app, scan it, and your agent wallet pairs with "
        "the bot. The QR expires in ~5 minutes — if it expires, press "
        "Connect Wallet again for a new one.\n\n"
        "2. <b>Check status</b> — shows your paired wallet address, "
        "BNB balance, Agent ID status and mint status. Press it after "
        "scanning the QR to finish pairing.\n\n"
        "3. <b>Mint Five Share</b> / <b>Mint Ten Share</b> — mints "
        "Agent ID (AI) shares from your paired wallet. I show the live "
        "cost first — you confirm before anything is sent. After the "
        "mint I immediately sell the AI you received and report what "
        "you got back (the sell is not a profit — it gets your BNB "
        "back, minus about $0.60 + gas).\n\n"
        "4. <b>Claim $3</b> — after your first mint through the bot, "
        "one $3 USDT claim per account.\n\n"
        "5. <b>Disconnect</b> — unpairs your wallet from the bot.\n\n"
        "You need: a .ai Agent ID in your wallet, enough BNB for the "
        "mint cost + gas, and Developer Mode ON in your Agentic "
        "Wallet settings (Binance app → Agentic Wallet → Settings → "
        "Developer Mode). Your wallet is only ever used when YOU tap "
        "a button — the bot never mints or moves funds on its own."
    )


# ---------------------------------------------------------- balance check

def bnb_balance(address):
    """BNB balance of an EVM address via a public BSC RPC."""
    resp = requests.post(
        BSC_RPC,
        json={"jsonrpc": "2.0", "method": "eth_getBalance",
              "params": [address, "latest"], "id": 1},
        timeout=15)
    wei = int(resp.json()["result"], 16)
    return wei / 1e18


def bnb_price_usd():
    """BNB price in USD from the Binance public ticker (no API key)."""
    resp = requests.get(BINANCE_TICKER, params={"symbol": "BNBUSDT"},
                        timeout=15)
    return float(resp.json()["price"])


# ---------------------------------------------------------------- tasks

def check_tasks(user_id):
    """Check membership of TASK_CHANNELS for a user.

    Returns a list of (channel, joined_or_None, note). joined is True when
    the user is a member/admin/creator, False when they left/were kicked,
    None when Telegram could not verify it (e.g. the bot is not an admin
    of the channel — getChatMember needs the bot to be a channel admin).
    """
    results = []
    for channel in TASK_CHANNELS:
        res = tg("getChatMember", {"chat_id": channel, "user_id": user_id})
        status = None
        try:
            status = res["result"]["status"]
        except (TypeError, KeyError):
            status = None
        if status in ("member", "administrator", "creator"):
            results.append((channel, True, None))
        elif status in ("left", "kicked"):
            results.append((channel, False, None))
        else:
            results.append((channel, None,
                            "could not verify — the bot must be an admin "
                            "of the channel"))
    return results


# ------------------------------------------------------- pairing + status

def _get_record(user_id):
    return baw_users.get_record(user_id)


def _save_record(user_id, record):
    baw_users.save_record(user_id, record)


def _expiry_message(chat_id):
    send_message(
        chat_id,
        "⌛ QR-এর মেয়াদ শেষ হয়ে গেছে।\nআবার Connect Wallet চাপুন।",
        main_keyboard())


def _send_qr(chat_id, user_id, qr_info):
    """Send a pairing QR image with its pairing code + instructions."""
    try:
        png = baw_users.make_qr_png(qr_info["qr_url"])
    except Exception as exc:
        send_message(chat_id,
                     "Could not make the QR image ({}).\n"
                     "Please try Connect Wallet again.".format(exc),
                     main_keyboard())
        return
    caption = (
        "Binance app দিয়ে scan করুন।\n"
        "Pairing code: <b>{}</b>\n"
        "QR-এর মেয়াদ ~5 মিনিট। Scan করার পর <b>Check status</b> চাপুন।"
    ).format(qr_info.get("pairing_code") or "—")
    send_photo(chat_id, png, caption=caption,
               reply_markup=main_keyboard())


def handle_connect(chat_id, user_id):
    record = _get_record(user_id)
    if record.get("paired_address"):
        send_message(
            chat_id,
            "Your wallet is already connected:\n"
            "<code>{}</code>\n\n"
            "Press Disconnect first if you want to pair a different "
            "wallet.".format(record["paired_address"]),
            main_keyboard())
        return
    pending = record.get("pending_qr") or {}
    now_ms = int(time.time() * 1000)
    if pending.get("qr_code_id") and pending.get("expires_at_ms", 0) > now_ms:
        # A live QR already exists — resend it instead of making a new one.
        send_message(chat_id, "⌛ Binance sign-in QR বানানো হচ্ছে...")
        _send_qr(chat_id, user_id, {
            "qr_url": pending["qr_url"],
            "pairing_code": pending.get("pairing_code", ""),
        })
        return
    if pending:
        record.pop("pending_qr", None)
        _save_record(user_id, record)
    send_message(chat_id, "⌛ Binance sign-in QR বানানো হচ্ছে...")
    try:
        qr_info = baw_users.signin(user_id)
    except BawError as exc:
        send_message(chat_id,
                     "Could not make the sign-in QR ({}).\n"
                     "Please try Connect Wallet again.".format(exc),
                     main_keyboard())
        return
    record["pending_qr"] = {
        "qr_code_id": qr_info["qr_code_id"],
        "qr_url": qr_info["qr_url"],
        "pairing_code": qr_info.get("pairing_code", ""),
        "expires_at_ms": qr_info["expires_at_ms"],
    }
    _save_record(user_id, record)
    _send_qr(chat_id, user_id, qr_info)


def _paired_status_lines(user_id, record):
    """Status lines for a paired wallet (address, BNB, Agent ID, mints)."""
    address = record.get("paired_address")
    lines = ["Connected wallet:\n<code>{}</code>".format(address)]
    try:
        bnb = baw_users.eth_get_balance(address) / 1e18
        lines.append("BNB: {:.5f}".format(bnb))
    except BawError:
        lines.append("BNB: (could not read right now)")
    claimed, agent_id = baw_users.get_agent_id(user_id)
    if claimed and agent_id:
        lines.append("Agent ID: {} ✅".format(agent_id))
    elif claimed:
        lines.append("Agent ID: claimed ✅")
    else:
        lines.append("Agent ID: not claimed ❌ — minting needs a .ai Agent ID")
    try:
        if baw_users.paid_once(address):
            lines.append("Minted before: yes")
    except BawError:
        pass
    minted = record.get("minted_count", 0)
    lines.append("Mints through this bot: {}".format(minted))
    lines.append("Claim $3: {}".format(
        "claimed ✅" if record.get("claimed")
        else ("available after your first mint" if minted
              else "available after your first mint")))
    return lines


def handle_status(chat_id, user_id):
    record = _get_record(user_id)
    pending = record.get("pending_qr") or {}
    now_ms = int(time.time() * 1000)
    if pending.get("qr_code_id"):
        if pending.get("expires_at_ms", 0) <= now_ms:
            record.pop("pending_qr", None)
            _save_record(user_id, record)
            _expiry_message(chat_id)
            return
        # A QR is waiting — the scan may have happened; verify it.
        try:
            baw_users.verify(user_id, pending["qr_code_id"])
        except BawError as exc:
            send_message(
                chat_id,
                "Not paired yet ({}).\n\n"
                "Scan the QR in the Binance app first, then press "
                "Check status again.".format(exc),
                main_keyboard())
            return
        address = baw_users.get_address(user_id)
        if not address:
            send_message(
                chat_id,
                "The scan was verified but I could not read your wallet "
                "address yet. Press Check status again in a bit.",
                main_keyboard())
            return
        record.pop("pending_qr", None)
        record["paired_address"] = address
        claimed, agent_id = baw_users.get_agent_id(user_id)
        if agent_id:
            record["agent_id"] = agent_id
        _save_record(user_id, record)
        send_message(chat_id,
                     "✅ Wallet connected!\n\n" +
                     "\n".join(_paired_status_lines(user_id, record)),
                     main_keyboard())
        return
    if record.get("paired_address"):
        send_message(chat_id,
                     "\n".join(_paired_status_lines(user_id, record)),
                     main_keyboard())
        return
    send_message(chat_id,
                 "No wallet connected yet.\n"
                 "Press Connect Wallet and scan the QR in the Binance app.",
                 main_keyboard())


def handle_disconnect(chat_id, user_id):
    record = _get_record(user_id)
    if not record.get("paired_address") and not record.get("pending_qr"):
        send_message(chat_id, "No wallet is connected.",
                     main_keyboard())
        return
    baw_users.signout(user_id)
    _save_record(user_id, {})
    send_message(chat_id,
                 "Disconnected. Your wallet is unpaired from the bot.\n"
                 "Press Connect Wallet to pair again.",
                 main_keyboard())


# ------------------------------------------------------------------ mint

def _fmt_bnb(wei):
    return "{:.5f}".format(wei / 1e18)


def handle_mint_quote(chat_id, user_id, qty):
    """Show the live numbers for a mint and ask the user to confirm.

    Nothing is sent yet — the mint only happens when the user taps
    Confirm Mint.
    """
    record = _get_record(user_id)
    address = record.get("paired_address")
    if not address:
        send_message(chat_id,
                     "No wallet connected yet.\n"
                     "Press Connect Wallet and scan the QR first.",
                     main_keyboard())
        return
    if address.lower() == baw_users.W1_ADDRESS.lower():
        send_message(chat_id,
                     "This wallet is the owner's wallet (W1) — it cannot "
                     "mint with itself as referrer. Pair your own agent "
                     "wallet instead.",
                     main_keyboard())
        return
    try:
        cost_wei = baw_users.cost_of(qty)
        total = baw_users.total_minted()
        bnb_wei = baw_users.eth_get_balance(address)
        ai_before = baw_users.token_balance(baw_users.AI_TOKEN, address)
        expected_ai = baw_users.estimate_ai_received(cost_wei, qty)
        amounts = baw_users.get_amounts_out(
            expected_ai, [baw_users.AI_TOKEN, baw_users.WBNB_TOKEN])
        sell_wei = amounts[1]
        price = bnb_price_usd()
    except BawError as exc:
        send_message(chat_id,
                     "Could not read the live mint numbers ({}).\n"
                     "Please try again in a bit.".format(exc),
                     main_keyboard())
        return
    cap_wei = qty * baw_users.MAX_COST_PER_SHARE_WEI
    if cost_wei > cap_wei:
        send_message(
            chat_id,
            "Mint stopped: the live cost is {} BNB for {} shares, above "
            "the 0.008 BNB/share cap. Try again later.".format(
                _fmt_bnb(cost_wei), qty),
            main_keyboard())
        return
    if bnb_wei < cost_wei + baw_users.GAS_RESERVE_WEI:
        send_message(
            chat_id,
            "Not enough BNB in your wallet.\n\n"
            "Mint cost: {} BNB\nYour BNB: {} BNB\n\n"
            "Top up your wallet first, then try again.".format(
                _fmt_bnb(cost_wei), _fmt_bnb(bnb_wei)),
            main_keyboard())
        return
    dev = baw_users.dev_mode_enabled(user_id)
    if dev is False:
        send_message(
            chat_id,
            "Developer Mode is OFF in your Agentic Wallet.\n\n"
            "Turn it on first (Binance app → Agentic Wallet → Settings "
            "→ Developer Mode), then try the mint again.",
            main_keyboard())
        return
    net_wei = sell_wei - cost_wei
    record["pending_mint"] = {
        "qty": qty,
        "cost_wei": cost_wei,
        "expected_start": total + 1,
        "ai_before_wei": ai_before,
        "quoted_at": int(time.time()),
    }
    _save_record(user_id, record)
    keyboard = {"inline_keyboard": [
        [{"text": _bold("Confirm Mint"),
          "callback_data": "confirm_mint_{}".format(qty)},
         {"text": _bold("Cancel"), "callback_data": "cancel_mint"}],
    ]}
    send_message(
        chat_id,
        "Mint {} shares — live quote:\n\n"
        "Cost: {} BNB (~${:.2f})\n"
        "You will receive: ~{:,} AI (estimate)\n"
        "Sell value: ~{} BNB\n"
        "Net: about {} BNB + gas\n\n"
        "The sell is NOT a profit — it gets your BNB back, minus "
        "about $0.60 + gas.\n\n"
        "Your wallet: <code>{}</code>\n"
        "Your BNB: {} BNB\n\n"
        "Press Confirm Mint to mint now.".format(
            qty, _fmt_bnb(cost_wei), cost_wei / 1e18 * price,
            expected_ai, _fmt_bnb(sell_wei),
            ("-" if net_wei < 0 else "+") + _fmt_bnb(abs(net_wei)),
            address, _fmt_bnb(bnb_wei)),
        keyboard)


def handle_mint_confirm(chat_id, user_id, qty):
    """Execute a quoted mint (preview first, then execute, then sell)."""
    record = _get_record(user_id)
    address = record.get("paired_address")
    pending = record.get("pending_mint") or {}
    if not address or pending.get("qty") != qty:
        send_message(chat_id,
                     "No live quote for this mint — press Mint {} again "
                     "to get a fresh quote.".format(
                         "Ten Share" if qty == 10 else "Five Share"),
                     main_keyboard())
        return
    if int(time.time()) - int(pending.get("quoted_at", 0)) > 120:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id,
                     "The quote expired (older than 2 minutes).\n"
                     "Press Mint {} again for a fresh quote.".format(
                         "Ten Share" if qty == 10 else "Five Share"),
                     main_keyboard())
        return
    home = baw_users.user_home(user_id)
    try:
        cost_wei = baw_users.cost_of(qty)
        total = baw_users.total_minted()
    except BawError as exc:
        send_message(chat_id,
                     "Could not re-read the live cost ({}). Mint stopped "
                     "— nothing was sent.".format(exc),
                     main_keyboard())
        return
    quoted_cost = int(pending["cost_wei"])
    cap_wei = qty * baw_users.MAX_COST_PER_SHARE_WEI
    if cost_wei > cap_wei or cost_wei > quoted_cost * 102 // 100:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(
            chat_id,
            "The live cost moved (now {} BNB vs quoted {} BNB). "
            "Mint stopped — nothing was sent. Press Mint {} again for "
            "a fresh quote.".format(
                _fmt_bnb(cost_wei), _fmt_bnb(quoted_cost),
                "Ten Share" if qty == 10 else "Five Share"),
            main_keyboard())
        return
    send_message(chat_id, "Minting {} shares...".format(qty))
    try:
        ai_before = baw_users.token_balance(baw_users.AI_TOKEN, address)
        bnb_before = baw_users.eth_get_balance(address)
        result = baw_users.do_mint(home, address, qty, cost_wei, total + 1)
    except BawError as exc:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(
            chat_id,
            "Mint failed: {}\n\nNothing was sent.".format(exc),
            main_keyboard())
        return
    mint_tx = result["tx_hash"]
    record.pop("pending_mint", None)
    record["minted_count"] = int(record.get("minted_count", 0)) + 1
    record["last_mint_tx"] = mint_tx
    record["first_mint_at"] = record.get("first_mint_at") or int(time.time())
    _save_record(user_id, record)
    # Instant sell of the AI just minted.
    sell_lines = []
    try:
        ai_after = baw_users.token_balance(baw_users.AI_TOKEN, address)
        received = ai_after - ai_before
        if received <= 0:
            sell_lines.append("No AI was received to sell.")
        else:
            sell = baw_users.sell_ai(home, address, received)
            bnb_after = baw_users.eth_get_balance(address)
            got_back = bnb_after - bnb_before + cost_wei
            sell_lines.append(
                "AI received: {:,} AI\n"
                "Sold for: {} BNB\n"
                "Sell tx: {}".format(
                    received, _fmt_bnb(got_back),
                    BSCSCAN_TX.format(sell["swap_tx"])))
    except BawError as exc:
        sell_lines.append(
            "Sell failed: {} — your AI stays in your wallet. "
            "You can sell it later from your wallet.".format(exc))
    send_message(
        chat_id,
        "✅ Minted {} shares!\n\n"
        "Mint tx: {}\n"
        "{}\n\n"
        "You can now press Claim $3 for your one-time reward.".format(
            qty, BSCSCAN_TX.format(mint_tx), "\n".join(sell_lines)),
        main_keyboard())


# ----------------------------------------------------------------- claim

def handle_claim(chat_id, user_id):
    record = _get_record(user_id)
    address = record.get("paired_address")
    if not address:
        send_message(chat_id,
                     "No wallet connected yet.\n"
                     "Press Connect Wallet and scan the QR first.",
                     main_keyboard())
        return
    if not record.get("minted_count"):
        send_message(chat_id,
                     "Claim $3 needs a mint first.\n"
                     "Mint Five Share or Mint Ten Share, then claim.",
                     main_keyboard())
        return
    if record.get("claimed"):
        send_message(chat_id,
                     "You already claimed your $3 reward.\n"
                     "One claim per account.",
                     main_keyboard())
        return
    amount_wei = int(CLAIM_USD * 10 ** baw_users.USDT_DECIMALS)
    try:
        pool = baw_users.w1_usdt_balance_wei()
    except BawError as exc:
        send_message(chat_id,
                     "Could not check the reward pool ({}).\n"
                     "Please try again in a bit.".format(exc),
                     main_keyboard())
        return
    if pool < amount_wei:
        send_message(
            chat_id,
            "The reward pool is empty right now (W1 holds ${:.2f} "
            "USDT, a claim needs ${:.2f}).\n\n"
            "Please contact the owner ({}) to top it up, then try "
            "again.".format(pool / 1e18, CLAIM_USD,
                            OWNER_CONTACT or "the owner"),
            main_keyboard())
        return
    send_message(chat_id, "Sending your ${:.2f} USDT reward...".format(CLAIM_USD))
    try:
        tx_hash = baw_users.pay_claim_usdt(address, amount_wei)
    except BawError as exc:
        send_message(
            chat_id,
            "Claim failed: {}\n\nYour claim is NOT used up — try again "
            "or contact the owner.".format(exc),
            main_keyboard())
        return
    record["claimed"] = True
    record["claimed_tx"] = tx_hash
    _save_record(user_id, record)
    send_message(
        chat_id,
        "✅ ${:.2f} USDT sent to <code>{}</code>!\n\n"
        "Tx: {}".format(CLAIM_USD, address, BSCSCAN_TX.format(tx_hash)),
        main_keyboard())


# -------------------------------------------------------------- handlers

def handle_start(chat_id, first_name=""):
    user_state.pop(chat_id, None)
    greeting = "Hi {}!\n\n".format(first_name) if first_name else ""
    send_message(chat_id, greeting + welcome_text(), main_keyboard())


def handle_callback(query):
    chat_id = query["message"]["chat"]["id"]
    user_id = query["from"]["id"]
    data = query.get("data", "")
    answer_callback(query["id"])

    if data == "connect_wallet":
        handle_connect(chat_id, user_id)
        return
    if data == "check_status":
        handle_status(chat_id, user_id)
        return
    if data == "mint_5":
        handle_mint_quote(chat_id, user_id, 5)
        return
    if data == "mint_10":
        handle_mint_quote(chat_id, user_id, 10)
        return
    if data == "confirm_mint_5":
        handle_mint_confirm(chat_id, user_id, 5)
        return
    if data == "confirm_mint_10":
        handle_mint_confirm(chat_id, user_id, 10)
        return
    if data == "cancel_mint":
        record = _get_record(user_id)
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id, "Mint cancelled — nothing was sent.",
                     main_keyboard())
        return
    if data == "claim_3":
        handle_claim(chat_id, user_id)
        return
    if data == "help":
        send_message(chat_id, help_text(), main_keyboard())
        return
    if data == "disconnect":
        handle_disconnect(chat_id, user_id)
        return

    if data == "check_balance":
        user_state[chat_id] = {"step": "awaiting_address"}
        send_message(
            chat_id,
            "Send your BSC wallet address (starts with 0x).\n"
            "I will check your BNB balance — no wallet connection needed, "
            "just the address.")
        return

    if data == "tasks":
        if not TASK_CHANNELS:
            send_message(chat_id,
                         "No required tasks — joining the channel is "
                         "optional.\nYou can mint now.",
                         main_keyboard())
            return
        results = check_tasks(user_id)
        lines = ["Tasks — join these channels:\n"]
        all_done = True
        for channel, joined, note in results:
            # Display: @handle for handles, "our channel" for numeric IDs.
            if channel.startswith("@"):
                display = "@" + channel.lstrip("@")
            elif channel.lstrip("-").isdigit():
                display = "our channel"
            else:
                display = channel
            # Join link: the invite link when set, else t.me/<handle>.
            if CHANNEL_INVITE_URL:
                join_link = CHANNEL_INVITE_URL
            elif channel.startswith("@"):
                join_link = "https://t.me/" + channel.lstrip("@")
            else:
                join_link = "https://t.me/" + channel
            if joined is True:
                lines.append("✅ {} — done".format(display))
            elif joined is False:
                all_done = False
                lines.append("❌ {} — not joined yet: {}"
                             .format(display, join_link))
            else:
                all_done = False
                lines.append("❓ {} — {}: {}"
                             .format(display, note, join_link))
        if all_done:
            lines.append("\nAll tasks done! You can mint now.")
        else:
            lines.append("\nJoin the channels above, then press Tasks again "
                         "to re-check.")
        send_message(chat_id, "\n".join(lines), main_keyboard())
        return


def handle_message(msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    first_name = (msg.get("from") or {}).get("first_name", "")

    if text.startswith("/start"):
        handle_start(chat_id, first_name)
        return

    state = user_state.get(chat_id, {})
    if state.get("step") == "awaiting_address":
        if not ADDR_RE.match(text):
            send_message(
                chat_id,
                "That does not look like a BSC wallet address.\n"
                "Send a 0x address (40 hex characters).")
            return
        user_state.pop(chat_id, None)
        try:
            balance = bnb_balance(text)
            price = bnb_price_usd()
            usd_value = balance * price
        except Exception:
            send_message(
                chat_id,
                "Could not check the balance right now (network error).\n"
                "Please try again in a bit.",
                main_keyboard())
            return
        if usd_value >= MIN_BNB_USD:
            verdict = "✅ Eligible!"
        else:
            verdict = "❌ Not eligible."
        send_message(
            chat_id,
            "{}\n\n"
            "BNB: {:.5f}\n"
            "Value: ${:.2f} (BNB price ${:.2f})\n"
            "Minimum needed: ${:.2f}".format(
                verdict, balance, usd_value, price, MIN_BNB_USD),
            main_keyboard())
        return

    send_message(chat_id, "Press /start to open the menu.", main_keyboard())


# ---------------------------------------------------------------- routes

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update:
        handle_message(update["message"])
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "mintbot"})


@app.route("/setwebhook", methods=["GET"])
def set_webhook():
    """Point Telegram at this app. Needs WEBHOOK_URL env var set to the
    public app URL, e.g. https://mintbot-xxxx.onrender.com"""
    if not WEBHOOK_URL:
        return jsonify({"ok": False,
                        "error": "WEBHOOK_URL env var is not set"}), 400
    url = WEBHOOK_URL.rstrip("/") + "/webhook"
    result = tg("setWebhook", {"url": url})
    return jsonify(result or {"ok": False, "error": "no response from Telegram"})


def run_polling():
    """Poll getUpdates in a loop (for running without a public URL).

    Used to run the bot on a machine with no webhook endpoint, e.g. to
    get it answering right away before the Render deploy. Clears any
    stale webhook first so polling receives the updates.
    """
    tg("deleteWebhook", {"drop_pending_updates": True})
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            res = tg("getUpdates", params)
            updates = (res or {}).get("result") or []
            for update in updates:
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
        except Exception as exc:  # network blip — wait and retry
            app.logger.warning("polling error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    if "--poll" in sys.argv or os.environ.get("POLLING") == "1":
        run_polling()
    else:
        port = int(os.environ.get("PORT", "5000"))
        app.run(host="0.0.0.0", port=port)
