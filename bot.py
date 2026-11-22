"""Mint referral bot — admin-style.

Users press /start, the bot checks they hold enough BNB, asks them to
complete tasks (join channels), and gives them Mint 5 / Mint 10 buttons
that open the bot's OWN mint page INSIDE Telegram as a Web App (Mini
App) — the user connects their own wallet there and mints, just like
the admin's bot. The bot itself never connects a wallet or mints.

The mint page is served by this app at GET /mint?qty=5 (or qty=10).
It calls the Agent ID (AI) mint contract on BNB Smart Chain directly,
so no external mint site is needed. Wallet connect in the page uses
WalletConnect (needs a WC_PROJECT_ID).

Webhook mode (for a free Render web service): Telegram sends updates to
POST /webhook. No long polling, no background process needed.

Config is env vars only — never hardcode a token, URL or channel handle:
    BOT_TOKEN       Telegram bot token from @BotFather
    MINT_URL        Fallback mint link template, {qty} is replaced by
                    5 or 10 (e.g. https://example.com/mint?ref=CODE
                    &qty={qty}). Only used when WEBHOOK_URL is NOT set;
                    when it is set, the Mint buttons open this app's
                    own /mint page instead.
    MINT_URL_5      Optional: separate mint link for the Mint 5 button
    MINT_URL_10     Optional: separate mint link for the Mint 10 button
                    (if both are set they are used instead of MINT_URL)
    WC_PROJECT_ID   WalletConnect Cloud project ID for the mint page's
                    wallet connect. Empty = wallet connect is not
                    configured yet; the page says so and cannot connect.
    ENABLE_INSTANT_SELL
                    "1" to auto-sell the AI tokens right after a mint
                    (default off). OFF unless the sell economics have
                    been verified live — a mint+sell can lose money.
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

Per-user state lives in memory only (user_state dict). A free Render
service restarts/sleeps wipe it — by design, nothing persistent is kept.
"""

import os
import re
import sys
import time
import requests
from flask import Flask, request, jsonify

from mint_page import render_mint_page

app = Flask(__name__)


# ---------------------------------------------------------------- config

def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def _env_bool(name, default=False):
    return (os.environ.get(name, "") or "").strip().lower() in (
        "1", "true", "yes", "on") if os.environ.get(name, "") else default


# ------------------------------------------------- Agent ID (AI) mint
# Verified on-chain (BNB Smart Chain, chainId 56). The mint contract is
# live; the old agentidai.meme site is dead, so the bot serves its own
# mint page instead of linking out to it.
MINT_CONTRACT = "0xa4bFa31f615F8e2a13dce3d93C0996CC5a56763D"
AI_TOKEN = "0x2a60AaB19b182B5C4b47fffC066ADA06Ab27924f"
WBNB_TOKEN = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
AI_PAIR = "0x6c3d8b927B8c38d5907aE64b17804871c97CEf97"
# Referrer for every mint from this bot: W1, the user's wallet. The
# referral bonus goes to W1. Never use any other referrer.
REFERRER_W1 = "0xE012133823df5452de9c10a73fE935d26B995A97"
# Hard cap on the mint price: 0.008 BNB per share (0.04 for 5, 0.08
# for 10). If costOf(qty) is above the cap, the mint page must stop.
MAX_COST_PER_SHARE_BNB = 0.008

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MINT_URL = os.environ.get("MINT_URL", "").strip()
MINT_URL_5 = os.environ.get("MINT_URL_5", "").strip()
MINT_URL_10 = os.environ.get("MINT_URL_10", "").strip()
WC_PROJECT_ID = os.environ.get("WC_PROJECT_ID", "").strip()
ENABLE_INSTANT_SELL = _env_bool("ENABLE_INSTANT_SELL", False)
MIN_BNB_USD = _env_float("MIN_BNB_USD", 41)
TASK_CHANNELS = [c.strip() for c in os.environ.get("TASK_CHANNELS", "").split(",") if c.strip()]
CHANNEL_INVITE_URL = os.environ.get("CHANNEL_INVITE_URL", "").strip()
OWNER_CONTACT = os.environ.get("OWNER_CONTACT", "").strip()
REWARD_TEXT = os.environ.get("REWARD_TEXT", "$3 per account").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
BSC_RPC = os.environ.get("BSC_RPC", "https://bsc-dataseed.bnbchain.org").strip()

TELEGRAM_API = "https://api.telegram.org/bot{}".format(BOT_TOKEN)
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

# chat_id -> {"step": "awaiting_address"} — memory only, wiped on restart.
user_state = {}

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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


def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload)


# ------------------------------------------------------------ mint links

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


def mint_url(qty):
    """URL for the Mint button of the given quantity (5 or 10).

    When WEBHOOK_URL is set, the button opens this app's own mint page
    at {WEBHOOK_URL}/mint?qty=N — no external mint site needed. When
    it is not set, fall back to the configured external mint links
    (MINT_URL_5/MINT_URL_10, else the MINT_URL template). Returns None
    when no mint link is configured at all.
    """
    if WEBHOOK_URL:
        return WEBHOOK_URL.rstrip("/") + "/mint?qty={}".format(qty)
    if MINT_URL_5 and MINT_URL_10:
        return MINT_URL_5 if qty == 5 else MINT_URL_10
    if MINT_URL:
        return MINT_URL.replace("{qty}", str(qty))
    return None


def main_keyboard():
    """Inline keyboard: Mint 5 / Mint 10, then Check Balance / Tasks.

    The Mint buttons are Telegram Web App (Mini App) buttons: tapping
    one opens the mint site INSIDE Telegram, where the user connects
    their own wallet and mints on the site — wallet connect + instant
    mint all happen in Telegram, like the admin's bot.
    """
    rows = []
    mint_row = []
    url5 = mint_url(5)
    url10 = mint_url(10)
    if url5:
        mint_row.append({"text": _bold("Mint 5"), "web_app": {"url": url5}})
    if url10:
        mint_row.append({"text": _bold("Mint 10"), "web_app": {"url": url10}})
    if mint_row:
        rows.append(mint_row)
    if CHANNEL_INVITE_URL:
        rows.append([{"text": _bold("Join Channel"), "url": CHANNEL_INVITE_URL}])
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
        "1. <b>Check Balance</b> — hold at least ${:.2f} worth of BNB.\n"
        "2. (Optional) <b>Join Channel</b>.\n"
        "3. <b>Mint 5</b> or <b>Mint 10</b> — our mint page opens inside "
        "Telegram, connect your wallet there and mint."
    ).format(REWARD_TEXT, MIN_BNB_USD)
    if mint_url(5) is None and mint_url(10) is None:
        text += "\n\nNote: the mint link is not set yet. Ask the owner to add it."
    if OWNER_CONTACT:
        text += "\n\nOwner: {}".format(OWNER_CONTACT)
    return text


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


@app.route("/mint", methods=["GET"])
def mint_page():
    """Serve the bot's own mint page (a Telegram Web App).

    The page connects the user's own wallet via WalletConnect, reads
    the live mint cost from the Agent ID mint contract, shows the exact
    numbers before the user confirms, and sends the mint. Only qty 5
    or 10 are accepted.
    """
    try:
        qty = int(request.args.get("qty", "10"))
    except (TypeError, ValueError):
        qty = 10
    if qty not in (5, 10):
        return jsonify({"ok": False,
                        "error": "qty must be 5 or 10"}), 400
    html = render_mint_page(qty, {
        "mint_contract": MINT_CONTRACT,
        "ai_token": AI_TOKEN,
        "wbnb_token": WBNB_TOKEN,
        "pancake_router": PANCAKE_ROUTER,
        "ai_pair": AI_PAIR,
        "referrer": REFERRER_W1,
        "wc_project_id": WC_PROJECT_ID,
        "enable_instant_sell": ENABLE_INSTANT_SELL,
        "max_cost_bnb": qty * MAX_COST_PER_SHARE_BNB,
        "bsc_rpc": BSC_RPC,
    })
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


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
