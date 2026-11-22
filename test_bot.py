"""Offline tests for bot.py — no real token, no calls to Telegram.

Run:  python test_bot.py
from the project folder (after pip install -r requirements.txt).

What it checks:
  A. /health returns 200.
  B. A simulated /start webhook update makes the bot send a message with
     Mint 5 and Mint 10 Web App buttons (MINT_URL template with {qty}).
  C. Same, but with MINT_URL_5 / MINT_URL_10 set instead.
  D. Balance flow: Check Balance -> address -> Eligible / Not eligible.
  E. Tasks flow with no channels configured -> optional, can mint now.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name
          + ((" — " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def load_bot(env):
    for key in ("BOT_TOKEN", "MINT_URL", "MINT_URL_5", "MINT_URL_10",
                "MIN_BNB_USD", "TASK_CHANNELS", "CHANNEL_INVITE_URL",
                "OWNER_CONTACT", "REWARD_TEXT", "WEBHOOK_URL",
                "WC_PROJECT_ID", "ENABLE_INSTANT_SELL"):
        if key in env:
            os.environ[key] = env[key]
        else:
            os.environ.pop(key, None)
    import bot
    importlib.reload(bot)
    return bot


def start_update(chat_id=111):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Test"},
            "text": "/start",
        },
    }


def buttons_of(calls):
    """Inline-keyboard buttons of the last sendMessage call."""
    sends = [p for m, p in calls if m == "sendMessage"]
    if not sends:
        return []
    kb = sends[-1].get("reply_markup", {}).get("inline_keyboard", [])
    return [b for row in kb for b in row]


def run_scenario(name, env, expected5, expected10):
    bot = load_bot(env)
    calls = []

    def fake_tg(method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    bot.tg = fake_tg
    client = bot.app.test_client()

    check(name + ": /health is 200",
          client.get("/health").status_code == 200)
    resp = client.post("/webhook", json=start_update())
    check(name + ": /webhook /start returns 200",
          resp.status_code == 200)
    btns = buttons_of(calls)
    urls = {b["text"]: (b.get("web_app") or {}).get("url") for b in btns
            if "web_app" in b}
    check(name + ": Mint 5 button Web App URL",
          urls.get(bot._bold("Mint 5")) == expected5, str(urls))
    check(name + ": Mint 10 button Web App URL",
          urls.get(bot._bold("Mint 10")) == expected10, str(urls))
    check(name + ": all button labels are bold",
          all(b["text"] and all(
              c == " " or "\U0001D400" <= c <= "\U0001D433"
              or "\U0001D7CE" <= c <= "\U0001D7D7"
              for c in b["text"]) for b in btns), str(btns))
    check(name + ": Check Balance callback button present",
          any(b.get("callback_data") == "check_balance" for b in btns))
    check(name + ": Tasks callback button present",
          any(b.get("callback_data") == "tasks" for b in btns))
    return bot, calls, client


# --- A/B: MINT_URL template with {qty} ------------------------------------
run_scenario(
    "template",
    {"BOT_TOKEN": "test-token",
     "MINT_URL": "https://example.com/mint?ref=TESTCODE&qty={qty}",
     "MIN_BNB_USD": "41", "TASK_CHANNELS": "",
     "REWARD_TEXT": "$3 per account"},
    "https://example.com/mint?ref=TESTCODE&qty=5",
    "https://example.com/mint?ref=TESTCODE&qty=10")

# --- C: separate MINT_URL_5 / MINT_URL_10 ----------------------------------
run_scenario(
    "separate-urls",
    {"BOT_TOKEN": "test-token",
     "MINT_URL": "",
     "MINT_URL_5": "https://example.com/mint5?ref=AAA",
     "MINT_URL_10": "https://example.com/mint10?ref=BBB",
     "MIN_BNB_USD": "41", "TASK_CHANNELS": "",
     "REWARD_TEXT": "$3 per account"},
    "https://example.com/mint5?ref=AAA",
    "https://example.com/mint10?ref=BBB")

# --- D: balance flow (mocked RPC + price) ----------------------------------
bot = load_bot({"BOT_TOKEN": "test-token",
                "MINT_URL": "https://example.com/mint?ref=X&qty={qty}",
                "MIN_BNB_USD": "41", "TASK_CHANNELS": ""})
calls = []
bot.tg = lambda m, p: calls.append((m, p)) or {"ok": True, "result": {}}
client = bot.app.test_client()

# 0.1 BNB at $500 = $50 -> eligible
bot.bnb_balance = lambda addr: 0.1
bot.bnb_price_usd = lambda: 500.0
client.post("/webhook", json={
    "update_id": 2,
    "callback_query": {"id": "q1", "from": {"id": 111},
                       "message": {"chat": {"id": 111}},
                       "data": "check_balance"}})
client.post("/webhook", json={
    "update_id": 3,
    "message": {"message_id": 2, "chat": {"id": 111},
                "from": {"id": 111},
                "text": "0xE012133823df5452de9c10a73fE935d26B995A97"}})
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("balance: eligible at $50 vs $41 min",
      any("Eligible" in t and "$50.00" in t for t in texts), str(texts))

# 0.01 BNB at $500 = $5 -> not eligible
calls.clear()
bot.bnb_balance = lambda addr: 0.01
client.post("/webhook", json={
    "update_id": 4,
    "callback_query": {"id": "q2", "from": {"id": 111},
                       "message": {"chat": {"id": 111}},
                       "data": "check_balance"}})
client.post("/webhook", json={
    "update_id": 5,
    "message": {"message_id": 3, "chat": {"id": 111},
                "from": {"id": 111},
                "text": "0xE012133823df5452de9c10a73fE935d26B995A97"}})
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("balance: not eligible at $5 vs $41 min",
      any("Not eligible" in t and "$5.00" in t for t in texts), str(texts))

# invalid address -> asked again, no crash
calls.clear()
client.post("/webhook", json={
    "update_id": 6,
    "callback_query": {"id": "q3", "from": {"id": 111},
                       "message": {"chat": {"id": 111}},
                       "data": "check_balance"}})
client.post("/webhook", json={
    "update_id": 7,
    "message": {"message_id": 4, "chat": {"id": 111},
                "from": {"id": 111}, "text": "not-an-address"}})
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("balance: invalid address rejected",
      any("does not look like" in t for t in texts), str(texts))

# --- E: tasks with no channels configured ----------------------------------
calls.clear()
client.post("/webhook", json={
    "update_id": 8,
    "callback_query": {"id": "q4", "from": {"id": 111},
                       "message": {"chat": {"id": 111}},
                       "data": "tasks"}})
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("tasks: no channels -> optional, can mint now",
      any("optional" in t and "You can mint now" in t for t in texts),
      str(texts))

# --- F: owner contact + join channel button ------------------------------
bot, calls, client = run_scenario(
    "owner-channel",
    {"BOT_TOKEN": "test-token",
     "MINT_URL": "https://example.com/mint?ref=TESTCODE&qty={qty}",
     "MIN_BNB_USD": "41", "TASK_CHANNELS": "",
     "CHANNEL_INVITE_URL": "https://t.me/+s2WrevCJwkFkOTVl",
     "OWNER_CONTACT": "@imzaman7",
     "REWARD_TEXT": "$3 per account"},
    "https://example.com/mint?ref=TESTCODE&qty=5",
    "https://example.com/mint?ref=TESTCODE&qty=10")
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("owner: welcome shows Owner @imzaman7",
      any("Owner: @imzaman7" in t for t in texts), str(texts))
btns = buttons_of(calls)
check("owner: Join Channel button with invite URL",
      any(b.get("text") == bot._bold("Join Channel")
          and b.get("url") == "https://t.me/+s2WrevCJwkFkOTVl"
          for b in btns), str(btns))
check("owner: welcome bolds the button names",
      any("<b>Mint 5</b>" in t and "<b>Check Balance</b>" in t
          for t in texts), str(texts))

# --- G: polling mode dispatches a /start update -------------------------
bot = load_bot({"BOT_TOKEN": "test-token",
                "MINT_URL": "https://example.com/mint?ref=X&qty={qty}",
                "MIN_BNB_USD": "41", "TASK_CHANNELS": "",
                "CHANNEL_INVITE_URL": "https://t.me/+s2WrevCJwkFkOTVl",
                "OWNER_CONTACT": "@imzaman7"})
calls = []
poll_state = {"n": 0}


def fake_poll_tg(method, payload):
    calls.append((method, payload))
    if method == "deleteWebhook":
        return {"ok": True, "result": True}
    if method == "getUpdates":
        poll_state["n"] += 1
        if poll_state["n"] == 1:
            return {"ok": True, "result": [start_update()]}
        raise SystemExit  # break the polling loop for the test
    return {"ok": True, "result": {}}


bot.tg = fake_poll_tg
try:
    bot.run_polling()
except SystemExit:
    pass
texts = [p["text"] for m, p in calls if m == "sendMessage"]
check("polling: /start update answered with welcome",
      any("Welcome" in t and "Owner: @imzaman7" in t for t in texts),
      str(texts))
check("polling: stale webhook cleared first",
      calls and calls[0][0] == "deleteWebhook", str(calls[:1]))

# --- H: own mint page (/mint) ----------------------------------------------
MINT_CONTRACT = "0xa4bFa31f615F8e2a13dce3d93C0996CC5a56763D"
REFERRER_W1 = "0xE012133823df5452de9c10a73fE935d26B995A97"

bot = load_bot({"BOT_TOKEN": "test-token", "MINT_URL": "",
                "WEBHOOK_URL": "https://mintbot-xxxx.onrender.com",
                "WC_PROJECT_ID": "", "ENABLE_INSTANT_SELL": ""})
client = bot.app.test_client()

resp = client.get("/mint?qty=5")
page5 = resp.get_data(as_text=True)
check("mint page: /mint?qty=5 returns 200",
      resp.status_code == 200, str(resp.status_code))
check("mint page: contains the mint contract address",
      MINT_CONTRACT in page5)
check("mint page: contains the W1 referrer",
      REFERRER_W1 in page5)
check("mint page: warns to use a regular wallet, not a baw agent wallet",
      "MetaMask" in page5 and "Agentic Wallet" in page5
      and "disconnect" in page5.lower())
check("mint page: warns wallet needs a .ai Agent ID and BNB for cost + gas",
      ".ai Agent ID" in page5 and "enough BNB" in page5)
check("mint page: WC unset -> 'not configured' warning shown",
      '<div class="error"><b>Wallet connect is not configured yet.</b>' in page5)
check("mint page: instant sell off by default",
      "enableInstantSell: false" in page5)

resp = client.get("/mint?qty=10")
page10 = resp.get_data(as_text=True)
check("mint page: /mint?qty=10 returns 200",
      resp.status_code == 200, str(resp.status_code))
check("mint page: qty=10 shows 10 shares",
      "Mint 10 Agent ID" in page10)
check("mint page: qty=10 cap is 0.08 BNB",
      '"0.08"' in page10 or "0.08" in page10)

resp = client.get("/mint?qty=7")
check("mint page: /mint?qty=7 returns 400",
      resp.status_code == 400, str(resp.status_code))

bot = load_bot({"BOT_TOKEN": "test-token", "MINT_URL": "",
                "WEBHOOK_URL": "https://mintbot-xxxx.onrender.com",
                "WC_PROJECT_ID": "test-project-id",
                "ENABLE_INSTANT_SELL": "1"})
client = bot.app.test_client()
resp = client.get("/mint?qty=5")
page_wc = resp.get_data(as_text=True)
check("mint page: WC set -> no 'not configured' warning",
      '<div class="error"><b>Wallet connect is not configured yet.</b>' not in page_wc)
check("mint page: instant sell on when ENABLE_INSTANT_SELL=1",
      "enableInstantSell: true" in page_wc)
check("mint page: no hardcoded bot token in page",
      "test-token" not in page_wc)

# --- I: Mint buttons point at the bot's own /mint page --------------------
# WEBHOOK_URL set, no MINT_URL: buttons use {WEBHOOK_URL}/mint?qty=N.
run_scenario(
    "own-mint-page",
    {"BOT_TOKEN": "test-token",
     "MINT_URL": "",
     "MIN_BNB_USD": "41", "TASK_CHANNELS": "",
     "WEBHOOK_URL": "https://mintbot-xxxx.onrender.com",
     "REWARD_TEXT": "$3 per account"},
    "https://mintbot-xxxx.onrender.com/mint?qty=5",
    "https://mintbot-xxxx.onrender.com/mint?qty=10")
# Fallback is covered by scenarios A/B above (no WEBHOOK_URL -> MINT_URL).

print()
if FAILURES:
    print("FAILED: {}".format(", ".join(FAILURES)))
    sys.exit(1)
print("ALL TESTS PASSED")
