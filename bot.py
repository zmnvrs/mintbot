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
qrCodeId and session state.

Minting goes through the Telegram Web App mini app (see mint_app.html):
the user connects their wallet there and sends the
mint transaction DIRECTLY on-chain — no baw preview, because the baw
preview simulation cannot simulate this mint (it fails with 351805
for every parameter, while a direct on-chain call succeeds). The
referrer is always W1, the owner's wallet. After a successful mint
the mini app reports the tx hash back to the bot, which records it
and immediately sells the AI just minted (instant reward) via baw.
If MINI_APP_URL is not set, the Mint buttons fall back to the old
baw quote/confirm flow (kept for when the mini app has no public
URL yet). The mini app also shows the live W1 referral slots
("🍬 Mint slots baki: left/total") and the W1 referral state, and
blocks the mint when no referral slots are left so nobody loses
gas on a sold-out referral.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "").strip()
REWARD_TEXT = os.environ.get("REWARD_TEXT", "$3 per account").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
BSC_RPC = os.environ.get("BSC_RPC", "https://bsc-dataseed.bnbchain.org").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
# Public base URL the mini app is served from, e.g.
# https://mintbot-xxxx.onrender.com. When set, the Mint buttons open
# the Telegram Web App mini app (direct wallet mint). When
# unset, the Mint buttons fall back to the baw quote/confirm flow.
# The wallet-connect project ID lives in WALLETCONNECT_PROJECT_ID (in
# .env, never committed); the /mint-app route injects it at runtime.
MINI_APP_URL = os.environ.get("MINI_APP_URL", "").strip()

TELEGRAM_API = "https://api.telegram.org/bot{}".format(BOT_TOKEN)
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BSCSCAN_TX = "https://bscscan.com/tx/{}"

# Crypto mystery-box sticker (user's regular sticker pack, black box with
# a red lid and "?") — sent once when Claim $3 is pressed.
BOX_STICKER_ID = ("CAACAgUAAxUAAWsCy_hy7dfjnYUabl1altfn9ZqcAAJXJgACHZMQ"
                  "VMEpAAFGr7pSyj0E")

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


def send_sticker(chat_id, sticker_file_id):
    """Send a sticker by file_id (e.g. the Crypto mystery-box sticker)."""
    return tg("sendSticker",
              {"chat_id": chat_id, "sticker": sticker_file_id})


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


# NOTE: premium custom-emoji icons (icon_custom_emoji_id) were tried for
# the buttons (ids from the user's packs + their own shield/box pack)
# but they do not render on the owner's Telegram, so the buttons use
# plain Unicode emojis in the text instead. The premium ids are kept
# in the project notes if they are ever needed again.


def _mint_button(qty):
    """The Mint button for qty shares.

    Plain callback button for the baw quote/confirm flow (the
    earlier system). The Web App mini app is OFF — the owner
    found it added hassle (a separate wallet connect that did
    not work), so Mint goes back to the bot's own flow.
    """
    label = "💰 Mint Ten Share" if qty == 10 else "💎 Mint Five Share"
    return {"text": _bold(label),
            "callback_data": "mint_{}".format(qty)}


def main_keyboard():
    """Inline keyboard — the admin bot's seven buttons, then the rest.

    Buttons use plain Unicode emojis in the text: the premium
    custom-emoji icons (icon_custom_emoji_id) do not render on the
    owner's Telegram, so the visible emoji is part of the label.
    """
    rows = [
        [{"text": _bold("🔗 Connect Wallet"), "callback_data": "connect_wallet"},
         {"text": _bold("✅ Check status"), "callback_data": "check_status"}],
        [_mint_button(10), _mint_button(5)],
        [{"text": _bold("🎁 Claim $3"), "callback_data": "claim_3"}],
        [{"text": _bold("❓ Help"), "callback_data": "help"},
         {"text": _bold("🚫 Disconnect"), "callback_data": "disconnect"}],
    ]
    if CHANNEL_INVITE_URL:
        rows.append([{"text": _bold("🛡 Join Channel"), "url": CHANNEL_INVITE_URL}])
    rows.append([
        {"text": _bold("💵 Check Balance"), "callback_data": "check_balance"},
        {"text": _bold("📌 Tasks"), "callback_data": "tasks"},
    ])
    return {"inline_keyboard": rows}


def help_keyboard():
    """Help keyboard: the main menu plus Owner + Admin contact buttons.

    Tapping one opens a chat with that person in Telegram, so users
    can message the owner or the admin directly for help.
    """
    rows = []
    contacts = []
    if OWNER_CONTACT:
        contacts.append(("👤 Owner ({})".format(OWNER_CONTACT),
                         OWNER_CONTACT))
    if ADMIN_CONTACT:
        contacts.append(("🛡 Admin ({})".format(ADMIN_CONTACT),
                         ADMIN_CONTACT))
    for label, handle in contacts:
        rows.append([{"text": _bold("💬 {}".format(label)),
                      "url": "https://t.me/{}".format(handle.lstrip("@"))}])
    rows.extend(main_keyboard()["inline_keyboard"])
    return {"inline_keyboard": rows}


def welcome_text():
    text = (
        "👋 <b>Welcome to Free Mint Crypto Cravers!</b>\n\n"
        "🎁 Earn {}.\n\n"
        "✨ <b>How it works:</b>\n"
        "1️⃣ 🔗 <b>Connect Wallet</b> — scan the Binance QR in the app to "
        "pair your agent wallet.\n"
        "2️⃣ 💰 <b>Mint Five Share</b> or <b>Mint Ten Share</b> — the "
        "mint app opens: connect your wallet there and confirm. The "
        "mint goes direct on-chain (2-3 seconds) and the referral "
        "goes to the owner.\n"
        "3️⃣ 🎁 <b>Claim $3</b> — after your first mint through the bot, "
        "one claim per account.\n\n"
        "⚠️ You need a .ai Agent ID in your wallet and enough BNB for the "
        "mint cost + gas. Turn on Developer Mode in your Agentic "
        "Wallet settings first."
    ).format(REWARD_TEXT)
    if OWNER_CONTACT:
        text += "\n\n👤 Owner: {}".format(OWNER_CONTACT)
    return text


def help_text():
    return (
        "❓ <b>How to use this bot:</b>\n\n"
        "1️⃣ 🔗 <b>Connect Wallet</b> — I send a Binance sign-in QR. Open "
        "the Binance app, scan it, and your agent wallet pairs with "
        "the bot. The QR expires in ~5 minutes — if it expires, press "
        "Connect Wallet again for a new one.\n\n"
        "2️⃣ ✅ <b>Check status</b> — shows your paired wallet address, "
        "BNB balance, Agent ID status and mint status. Press it after "
        "scanning the QR to finish pairing.\n\n"
        "3️⃣ 💰 <b>Mint Five Share</b> / <b>Mint Ten Share</b> — the "
        "mint app opens and shows the live cost, the slots left and "
        "the referral. Connect your wallet in the app and confirm — "
        "the mint goes direct on-chain (2-3 seconds). After the mint "
        "I immediately sell the AI you received and report what you "
        "got back (the sell is not a profit — it gets your BNB back, "
        "minus about $0.60 + gas). If no slots are left the app "
        "blocks the mint so you lose no gas.\n\n"
        "4️⃣ 🎁 <b>Claim $3</b> — after your first mint through the bot, "
        "one $3 USDT claim per account.\n\n"
        "5️⃣ 🚫 <b>Disconnect</b> — unpairs your wallet from the bot.\n\n"
        "💬 <b>Need help?</b> Tap a button below to message the owner "
        "({}) or the admin ({}) directly.\n\n"
        "⚠️ You need: a .ai Agent ID in your wallet, enough BNB for the "
        "mint cost + gas, and Developer Mode ON in your Agentic "
        "Wallet settings (Binance app → Agentic Wallet → Settings → "
        "Developer Mode). Your wallet is only ever used when YOU tap "
        "a button — the bot never mints or moves funds on its own."
    ).format(OWNER_CONTACT or "the owner",
             ADMIN_CONTACT or "the admin")


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


# BNB price cache (120s) — the Binance ticker is slow/geo-blocked from
# this machine, so repeated status/balance checks reuse one price.
_price_cache = {"value": None, "at": 0.0}
_PRICE_TTL_S = 120


def bnb_price_usd():
    """BNB price in USD. Tries the Binance ticker first, then CoinGecko
    (api.binance.com is geo-blocked from this machine and returns an
    error instead of a price)."""
    now = time.time()
    if (_price_cache["value"] is not None
            and now - _price_cache["at"] < _PRICE_TTL_S):
        return _price_cache["value"]
    value = None
    try:
        resp = requests.get(BINANCE_TICKER, params={"symbol": "BNBUSDT"},
                            timeout=15)
        value = float(resp.json()["price"])
    except Exception:
        value = None
    if value is None:
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "binancecoin", "vs_currencies": "usd"},
                timeout=15)
            value = float(resp.json()["binancecoin"]["usd"])
        except Exception:
            value = None
    if value is None:
        raise RuntimeError("could not fetch the BNB price")
    _price_cache["value"] = value
    _price_cache["at"] = now
    return value


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
    """Status lines for a paired wallet (address, BNB, Agent ID, mints).

    The three baw reads run in parallel — each spawns a subprocess and
    takes seconds, so running them one after another made Check status
    feel very slow.
    """
    address = record.get("paired_address")
    lines = ["Connected wallet:\n<code>{}</code>".format(address)]

    def _balance():
        try:
            return baw_users.eth_get_balance(address) / 1e18
        except BawError:
            return None

    def _agent():
        try:
            return baw_users.get_agent_id(user_id)
        except BawError:
            return (False, None)

    def _paid():
        try:
            return baw_users.paid_once(address)
        except BawError:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_balance = pool.submit(_balance)
        fut_agent = pool.submit(_agent)
        fut_paid = pool.submit(_paid)
        bnb = fut_balance.result()
        claimed, agent_id = fut_agent.result()
        paid_before = fut_paid.result()
    if bnb is None:
        lines.append("BNB: (could not read right now)")
    else:
        lines.append("BNB: {:.5f}".format(bnb))
    if claimed and agent_id:
        lines.append("Agent ID: {} ✅".format(agent_id))
    elif claimed:
        lines.append("Agent ID: claimed ✅")
    else:
        lines.append("Agent ID: not claimed ❌ — minting needs a .ai Agent ID")
    if paid_before:
        lines.append("Minted before: yes")
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
        # On-chain fallback: if the wallet minted (e.g. via the mini
        # app) but the bot never got the report, paidOnce proves it.
        # Record it once so Claim $3 unlocks; never double-count.
        if not record.get("minted_count"):
            try:
                if baw_users.paid_once(record["paired_address"]):
                    record["minted_count"] = 1
                    record["minted_onchain"] = True
                    _save_record(user_id, record)
            except BawError:
                pass
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


def _referral_line(ref):
    """One-line W1 referral summary for the quote / slots messages.

    ref is baw_users.referral_stats(): level, invites, owed_wei
    (each may be None when its chain read failed).
    """
    level = ref.get("level")
    invites = ref.get("invites")
    owed = ref.get("owed_wei")
    level_txt = ("Level {} ({}%)".format(level, {0: 5, 1: 10, 2: 15}.get(level, 5))
                 if level is not None else "Level —")
    invites_txt = ("{} invites".format(invites)
                   if invites is not None else "invites —")
    owed_txt = ("{:,} AI owed".format(owed)
                if owed is not None else "owed —")
    return "👥 Referral (W1): {} • {} • {}".format(
        level_txt, invites_txt, owed_txt)


def _slots_lines(total, minted, left):
    """Referral slots line for the quote (W1's referral quota).

    Format matches the official bot: "🍬 Mint slots baki: 2187/2197".
    total/minted/left are W1's referral quota, NOT the global supply.
    left None means the quota reads failed — show "—", never block.
    """
    if left is None:
        return "🍬 Mint slots baki: —"
    return "🍬 Mint slots baki: {:,}/{:,} (mint hoyeche {:,})".format(
        left, total, minted)


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
        # The independent chain reads run in parallel — one after another
        # they took seconds each and the quote felt very slow.
        with ThreadPoolExecutor(max_workers=8) as pool:
            fut_cost = pool.submit(baw_users.cost_of, qty)
            fut_total = pool.submit(baw_users.total_minted)
            fut_bnb = pool.submit(baw_users.eth_get_balance, address)
            fut_ai = pool.submit(baw_users.token_balance,
                                 baw_users.AI_TOKEN, address)
            fut_usdt = pool.submit(baw_users.token_balance,
                                   baw_users.USDT_TOKEN, address)
            fut_gas = pool.submit(baw_users.estimate_mint_gas_wei)
            fut_slots = pool.submit(baw_users.referral_slots)
            fut_ref = pool.submit(baw_users.referral_stats)
            cost_wei = fut_cost.result()
            total = fut_total.result()
            bnb_wei = fut_bnb.result()
            ai_before = fut_ai.result()
            usdt_wei = fut_usdt.result()
            gas_wei = fut_gas.result()
            # Referral slots + referral must never kill the quote: if
            # their reads fail, show them as unavailable and do not
            # block. slots is (total, minted, left) or None.
            try:
                slots = fut_slots.result()
            except BawError:
                slots = None
            try:
                ref = fut_ref.result()
            except BawError:
                ref = {"level": None, "invites": None, "owed_wei": None}
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
    # Slots here are W1's REFERRAL slots (quotaOf - mintedCount), not
    # the global mint supply. slots is (total, minted, left) or None
    # when the quota reads failed (then never block).
    if slots is not None:
        ref_total, ref_minted, ref_left = slots
    else:
        ref_total = ref_minted = ref_left = None
    ref_line = _referral_line(ref)
    if ref_left is not None and ref_left < qty:
        # No referral slots left — block the mint so nobody loses
        # gas/funds on a sold-out referral. Slots come back only if
        # the project adds more quota; until then the mint stays
        # blocked.
        send_message(
            chat_id,
            "❌ Slot nei — mint kora jabe na.\n\n"
            "Referral-e ar kono slot nei — mint kora jabe na.\n"
            "Tumi chaiso: {} shares, baki ache: {} slots.\n\n"
            "🍬 Mint slots baki: {:,}/{:,}\n"
            "✅ Mint hoyeche: {:,}\n"
            "{}\n\n"
            "Slot add hole abar Mint চাপুন।".format(
                qty, ref_left, ref_left, ref_total, ref_minted, ref_line),
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
    total_needed_wei = cost_wei + gas_wei
    if bnb_wei < total_needed_wei:
        short_wei = total_needed_wei - bnb_wei
        usdt_hint = ""
        if usdt_wei > 0:
            usdt_hint = (
                "\n\n💵 আপনার wallet এ {:.2f} USDT আছে — swap করে BNB "
                "নিতে পারেন।".format(usdt_wei / 10 ** 18))
        send_message(
            chat_id,
            "❌ BNB কম আছে।\n\n"
            "Wallet: <code>{}</code>\n"
            "Balance: {} BNB\n"
            "Mint cost: {} BNB\n"
            "Gas: ~{} BNB\n"
            "লাগবে: ~{} BNB (${:.2f})\n"
            "আর লাগবে: {} BNB (~${:.2f}){}"
            "\n\nBNB top-up করে আবার Mint চাপুন।".format(
                address, _fmt_bnb(bnb_wei),
                _fmt_bnb(cost_wei), _fmt_bnb(gas_wei),
                _fmt_bnb(total_needed_wei),
                total_needed_wei / 1e18 * price,
                _fmt_bnb(short_wei), short_wei / 1e18 * price,
                usdt_hint),
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
        "💰 Cost: {} BNB (~${:.2f})\n"
        "⛽ Gas: ~{} BNB\n"
        "💲 মোট লাগবে: {} BNB\n"
        "You will receive: ~{:,} AI\n"
        "Sell value: ~{} BNB\n"
        "Net: about {} BNB + gas\n\n"
        "{}\n"
        "{}\n\n"
        "Sell করলে profit হবে না — BNB ফেরত পাবেন, ~$0.60 + gas কম।\n\n"
        "Wallet: <code>{}</code>\n"
        "Your BNB: {} BNB\n\n"
        "Confirm Mint চাপুন।".format(
            qty, _fmt_bnb(cost_wei), cost_wei / 1e18 * price,
            _fmt_bnb(gas_wei), _fmt_bnb(total_needed_wei),
            expected_ai, _fmt_bnb(sell_wei),
            ("-" if net_wei < 0 else "+") + _fmt_bnb(abs(net_wei)),
            _slots_lines(ref_total, ref_minted, ref_left), ref_line,
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
    # Prompt checks before sending (the mint prompt's CHECKS section):
    # 1. wallet owns an Agent ID; 2. never minted before (paidOnce
    # false); 3. BNB >= maxCost + 0.001 for gas. Stop if any fails.
    try:
        claimed, agent_id = baw_users.get_agent_id(user_id)
        already_minted = baw_users.paid_once(address)
        bnb_wei = baw_users.eth_get_balance(address)
    except BawError as exc:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id,
                     "Could not verify the wallet ({}). Mint stopped "
                     "— nothing was sent.".format(exc),
                     main_keyboard())
        return
    if not agent_id:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id,
                     "No Agent ID found in this wallet — the mint needs "
                     "a .ai Agent ID. Mint stopped — nothing was sent.",
                     main_keyboard())
        return
    if already_minted:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id,
                     "This wallet already minted Agent ID before "
                     "(paidOnce is true). Mint stopped — nothing was sent.",
                     main_keyboard())
        return
    if bnb_wei < cost_wei + 10 ** 15:
        record.pop("pending_mint", None)
        _save_record(user_id, record)
        send_message(chat_id,
                     "BNB kom ache — mint er jonno {} BNB + gas lagbe, "
                     "wallet e ache {} BNB. Mint stopped — nothing was "
                     "sent.".format(_fmt_bnb(cost_wei),
                                   _fmt_bnb(bnb_wei)),
                     main_keyboard())
        return
    send_message(chat_id,
                 "⏳ Mint করা হচ্ছে...\n"
                 "Binance app-এ approve করুন।")
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
    mint_tx = result.get("tx_hash")
    tx_line = ("Tx: {}\n{}".format(mint_tx, BSCSCAN_TX.format(mint_tx))
               if mint_tx else
               "Tx: Binance app-এ approve করা হয়েছে (hash ধরা যায়নি) — "
               "Check status-এ দেখুন।")
    record.pop("pending_mint", None)
    record["minted_count"] = int(record.get("minted_count", 0)) + 1
    record["total_shares"] = int(record.get("total_shares", 0)) + qty
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
            try:
                sell_usd = got_back / 1e18 * bnb_price_usd()
                sell_usd_txt = " (${:.2f})".format(sell_usd)
            except Exception:
                sell_usd_txt = ""
            sell_lines.append(
                "💰 Sell সফল!\n"
                "AI বিক্রি: {:,}\n"
                "BNB ফেরত: {} BNB{}\n"
                "Sell Tx: {}\n"
                "{}".format(
                    received, _fmt_bnb(got_back), sell_usd_txt,
                    sell["swap_tx"], BSCSCAN_TX.format(sell["swap_tx"])))
    except BawError as exc:
        sell_lines.append(
            "Sell failed: {} — your AI stays in your wallet. "
            "You can sell it later from your wallet.".format(exc))
    send_message(
        chat_id,
        "✅ Mint সফল!\n\n"
        "Shares: {} (মোট আপনার: {})\n"
        "{}\n\n"
        "{}\n\n"
        "Claim $3 চাপুন — আপনার one-time reward নিন।".format(
            qty, record["total_shares"], tx_line, "\n".join(sell_lines)),
        main_keyboard())


def handle_web_app_data(chat_id, user_id, data):
    """Record a mint the mini app reports (direct wallet mint).

    data is the JSON string the mini app sent via Telegram.WebApp
    .sendData: {"type": "mint", "tx_hash": "0x...", "qty": 5,
    "address": "0x..."}. Records minted_count/total_shares (once per
    tx hash, so a re-sent report never double-counts), then runs the
    instant sell via baw when the reported wallet is the paired
    wallet (the baw session can only sell from the paired wallet).
    Enables Claim $3, exactly like a baw mint.
    """
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        send_message(chat_id,
                     "Could not read the mint report from the app.\n"
                     "Tap Check status — if the mint is on-chain I will "
                     "record it there.",
                     main_keyboard())
        return
    if not isinstance(payload, dict) or payload.get("type") != "mint":
        return
    tx_hash = payload.get("tx_hash") or ""
    address = payload.get("address") or ""
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
        send_message(chat_id,
                     "The app did not send a valid mint tx hash.\n"
                     "Tap Check status to verify the mint on-chain.",
                     main_keyboard())
        return
    try:
        qty = int(payload.get("qty"))
    except (TypeError, ValueError):
        qty = 0
    if qty not in (5, 10):
        qty = 0
    if not isinstance(address, str) or not ADDR_RE.match(address):
        address = ""
    record = _get_record(user_id)
    if record.get("last_mint_tx") == tx_hash:
        # Already recorded (the app re-sent) — do not double-count.
        send_message(chat_id,
                     "Mint already recorded ✅\nTx: {}\n{}".format(
                         tx_hash, BSCSCAN_TX.format(tx_hash)),
                     main_keyboard())
        return
    paired = record.get("paired_address") or ""
    record["minted_count"] = int(record.get("minted_count", 0)) + 1
    record["total_shares"] = int(record.get("total_shares", 0)) + qty
    record["last_mint_tx"] = tx_hash
    record["first_mint_at"] = record.get("first_mint_at") or int(time.time())
    if address:
        record["last_mint_address"] = address
    _save_record(user_id, record)
    # Instant sell via baw — only possible from the paired wallet.
    sell_lines = []
    if address and paired and address.lower() != paired.lower():
        sell_lines.append(
            "Mint wallet ({}) is not the wallet paired with the bot "
            "({}). The instant sell needs the paired wallet — your AI "
            "stays in your wallet, sell it later from there.".format(
                address, paired))
    elif not paired:
        sell_lines.append(
            "No wallet is paired with the bot, so the instant sell "
            "could not run. Your AI stays in your wallet — pair it "
            "with Connect Wallet, then sell from there.")
    else:
        home = baw_users.user_home(user_id)
        try:
            ai_balance = baw_users.token_balance(baw_users.AI_TOKEN, paired)
            bnb_before = baw_users.eth_get_balance(paired)
            if ai_balance <= 0:
                sell_lines.append(
                    "No AI in the paired wallet to sell yet — it may "
                    "still be confirming. Check status in a bit.")
            else:
                sell = baw_users.sell_ai(home, paired, ai_balance)
                bnb_after = baw_users.eth_get_balance(paired)
                got_back = bnb_after - bnb_before
                try:
                    sell_usd = got_back / 1e18 * bnb_price_usd()
                    sell_usd_txt = " (${:.2f})".format(sell_usd)
                except Exception:
                    sell_usd_txt = ""
                sell_lines.append(
                    "💰 Sell সফল!\n"
                    "AI বিক্রি: {:,}\n"
                    "BNB ফেরত: {} BNB{}\n"
                    "Sell Tx: {}\n"
                    "{}".format(
                        ai_balance, _fmt_bnb(got_back), sell_usd_txt,
                        sell["swap_tx"], BSCSCAN_TX.format(sell["swap_tx"])))
        except BawError as exc:
            sell_lines.append(
                "Sell failed: {} — your AI stays in your wallet. "
                "You can sell it later from your wallet.".format(exc))
    send_message(
        chat_id,
        "✅ Mint সফল!\n\n"
        "Shares: {} (মোট আপনার: {})\n"
        "Tx: {}\n"
        "{}\n\n"
        "{}\n\n"
        "Claim $3 চাপুন — আপনার one-time reward নিন।".format(
            qty, record["total_shares"], tx_hash,
            BSCSCAN_TX.format(tx_hash), "\n".join(sell_lines)),
        main_keyboard())


# ----------------------------------------------------------------- claim

def handle_claim(chat_id, user_id):
    # The Crypto mystery-box sticker, once per Claim press.
    send_sticker(chat_id, BOX_STICKER_ID)
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
    # Answer immediately so the button stops spinning at once; for the
    # mint buttons also show a toast — the live quote takes a few
    # seconds of chain reads, and silence until then feels slow.
    if data in ("mint_5", "mint_10"):
        answer_callback(query["id"], "⌛ Mint-এর হিসাব করা হচ্ছে...")
    else:
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
        send_message(chat_id, help_text(), help_keyboard())
        return
    if data == "disconnect":
        handle_disconnect(chat_id, user_id)
        return

    if data == "check_balance":
        user_state[chat_id] = {"step": "awaiting_address"}
        send_message(
            chat_id,
            "Send your BSC wallet address (starts with 0x).\n"
            "I will show your BNB, your USDT, and your BNB value in "
            "USDT — no wallet connection needed, just the address.")
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

    # The mini app reports a successful direct mint back to the bot.
    web_app_data = msg.get("web_app_data") or {}
    if web_app_data.get("data"):
        handle_web_app_data(chat_id, msg.get("from", {}).get("id"),
                            web_app_data["data"])
        return

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
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_bnb = pool.submit(bnb_balance, text)
                fut_usdt = pool.submit(
                    baw_users.token_balance, baw_users.USDT_TOKEN, text)
                balance = fut_bnb.result()
                usdt = fut_usdt.result() / 1e18
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
            "💰 BNB: {:.5f}\n"
            "💵 USDT: {:.2f}\n"
            "💲 BNB value: ${:.2f} (BNB price ${:.2f})\n"
            "Minimum needed: ${:.2f}".format(
                verdict, balance, usdt, usd_value, price, MIN_BNB_USD),
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


@app.route("/static/<path:filename>", methods=["GET"])
def static_files(filename):
    """Serve the mini app's wallet libraries (ethers + the WC provider).

    Served locally from the project's static/ dir (same origin) so
    the page does not depend on a public CDN.
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    path = os.path.normpath(os.path.join(base, filename))
    if not path.startswith(base) or not os.path.isfile(path):
        return "Not found", 404
    mimetype = "application/javascript" if path.endswith(".js") else None
    with open(path, "rb") as fh:
        data = fh.read()
    return data, 200, {"Content-Type": mimetype or "application/octet-stream"}


@app.route("/mint-app", methods=["GET"])
def mint_app():
    """Serve the Telegram Web App mini app (direct wallet mint).

    The HTML file holds only placeholders — the wallet-connect project
    ID and the contract addresses are injected here at runtime from
    the environment / baw_users, so no secret is ever committed in
    the page. Without a project ID the page cannot connect a wallet,
    so it answers with a plain error page instead.
    """
    project_id = os.environ.get("WALLETCONNECT_PROJECT_ID", "").strip()
    if not project_id:
        return ("<h3>Mini app is not configured</h3>"
                "<p>The owner has not set the wallet-connect project ID "
                "yet. Please use the Mint buttons in the bot, or ask "
                "the owner to configure it.</p>"), 503
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mint_app.html")
    try:
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
    except OSError:
        return ("<h3>Mini app is missing</h3>"
                "<p>mint_app.html was not found on the server.</p>"), 500
    html = (html
            .replace("__WALLETCONNECT_PROJECT_ID__", project_id)
            .replace("__MINT_CONTRACT__", baw_users.MINT_CONTRACT)
            .replace("__REFERRER_W1__", baw_users.REFERRER_W1)
            .replace("__AI_TOKEN__", baw_users.AI_TOKEN)
            .replace("__WBNB_TOKEN__", baw_users.WBNB_TOKEN)
            .replace("__PANCAKE_ROUTER__", baw_users.PANCAKE_ROUTER)
            .replace("__CHAIN_ID__", str(baw_users.CHAIN_ID)))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/quote", methods=["GET"])
def api_quote():
    """Live mint quote as JSON for the mini app.

    ?qty=5|10. Returns cost, gas, expected AI, sell value, W1's
    REFERRAL slots (total / minted / left) and the W1 referral state,
    so the mini app can show them and block the mint when no referral
    slots are left. The global mint supply is included only as a
    secondary reference (global_total_minted / global_slots_left).
    """
    try:
        qty = int(request.args.get("qty", "5"))
    except (TypeError, ValueError):
        qty = 0
    if qty not in (5, 10):
        return jsonify({"ok": False,
                        "error": "qty must be 5 or 10"}), 400
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            fut_cost = pool.submit(baw_users.cost_of, qty)
            fut_total = pool.submit(baw_users.total_minted)
            fut_gas = pool.submit(baw_users.estimate_mint_gas_wei)
            fut_ref = pool.submit(baw_users.referral_stats)
            fut_slots = pool.submit(baw_users.referral_slots)
            cost_wei = fut_cost.result()
            total = fut_total.result()
            gas_wei = fut_gas.result()
            ref = fut_ref.result()
            # Referral quota reads must never kill the quote: on
            # failure the slots come back null and the mini app shows
            # "—" and does NOT block.
            try:
                ref_total, ref_minted, ref_left = fut_slots.result()
            except BawError:
                ref_total = ref_minted = ref_left = None
        expected_ai = baw_users.estimate_ai_received(cost_wei, qty)
        amounts = baw_users.get_amounts_out(
            expected_ai, [baw_users.AI_TOKEN, baw_users.WBNB_TOKEN])
        sell_wei = amounts[1]
        try:
            price = bnb_price_usd()
        except Exception:
            price = None
    except BawError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({
        "ok": True,
        "qty": qty,
        "cost_wei": cost_wei,
        "cost_wei_str": str(cost_wei),
        "cost_bnb": cost_wei / 1e18,
        "gas_wei": gas_wei,
        "gas_wei_str": str(gas_wei),
        "gas_bnb": gas_wei / 1e18,
        "expected_ai": expected_ai,
        "expected_ai_str": str(expected_ai),
        "sell_wei": sell_wei,
        "sell_wei_str": str(sell_wei),
        "sell_bnb": sell_wei / 1e18,
        "total_minted": ref_minted,
        "total_shares": ref_total,
        "slots_left": ref_left,
        "referral_slots": {"total": ref_total, "minted": ref_minted,
                           "left": ref_left},
        "global_total_minted": total,
        "global_slots_left": max(0, baw_users.TOTAL_SHARES - total),
        "referral": ref,
        "bnb_price_usd": price,
        "referrer": baw_users.REFERRER_W1,
        "mint_contract": baw_users.MINT_CONTRACT,
        "chain_id": baw_users.CHAIN_ID,
    })


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


def _dispatch_update(update):
    """Handle one update (callback or message). Runs in its own thread
    so a slow handler (baw subprocess calls take seconds) never blocks
    the next button press."""
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as exc:  # never let one update kill the loop
        app.logger.warning("update handler failed: %s", exc)


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
                threading.Thread(target=_dispatch_update, args=(update,),
                                 daemon=True).start()
        except Exception as exc:  # network blip — wait and retry
            app.logger.warning("polling error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    if "--poll" in sys.argv or os.environ.get("POLLING") == "1":
        run_polling()
    else:
        port = int(os.environ.get("PORT", "5000"))
        app.run(host="0.0.0.0", port=port)
