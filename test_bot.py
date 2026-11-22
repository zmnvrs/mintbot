"""Offline tests for bot.py + baw_users.py — no real token, no real
wallet, no calls to Telegram or to the baw CLI (baw is mocked).

Run:  python test_bot.py
from the project folder (after pip install -r requirements.txt).

What it checks:
  A. /health returns 200.
  B. /start shows the admin bot's seven buttons (Connect Wallet,
     Check status, Mint Ten Share, Mint Five Share, Claim $3, Help,
     Disconnect) as callback buttons — no Web App, no WalletConnect.
  C. The /mint route is gone (404) and bot.py has no Web App /
     WalletConnect / mint_page references.
  D. Connect Wallet flow with mocked baw: QR image sent as a photo,
     pending QR stored, pairing code shown.
  E. Check status: pending QR verifies -> paired; expired QR -> the
     Bengali expiry message; not scanned yet -> "Not paired yet".
  F. Per-user session isolation: two users get different baw HOMEs.
  G. Mint quote + confirm with mocked baw/chain reads: live numbers
     shown, confirm mints + sells, tx hashes reported; quote expiry,
     cost cap, insufficient BNB, and preview failure all stop cleanly.
  H. Claim $3: needs a mint first; once per account; empty W1 reward
     pool -> plain message, claim NOT used up; success marks claimed.
  I. Disconnect unpairs and clears state. Help explains the flow.
  J. Balance flow (address -> Eligible / Not eligible) and Tasks flow
     with no channels -> optional, can mint now.
  K. Polling mode dispatches a /start update.
"""

import importlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name
          + ((" — " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def load_bot(env):
    """Load bot + baw_users with the given env (fresh temp state dir)."""
    tmp = tempfile.mkdtemp(prefix="mintbot-test-")
    env = dict(env)
    env.setdefault("BAW_USERS_HOME", os.path.join(tmp, "baw-users"))
    env.setdefault("BAW_STATE_FILE", os.path.join(tmp, "state.json"))
    env.setdefault("W1_BAW_HOME", os.path.join(tmp, "w1"))
    for key in ("BOT_TOKEN", "CLAIM_USD", "MIN_BNB_USD", "TASK_CHANNELS",
                "CHANNEL_INVITE_URL", "OWNER_CONTACT", "ADMIN_CONTACT",
                "REWARD_TEXT",
                "WEBHOOK_URL", "BSC_RPC", "BAW_USERS_HOME", "W1_BAW_HOME",
                "BAW_STATE_FILE", "BAW_BIN"):
        if key in env:
            os.environ[key] = env[key]
        else:
            os.environ.pop(key, None)
    import baw_users
    import bot
    importlib.reload(baw_users)
    importlib.reload(bot)
    return bot, baw_users


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


def callback_update(chat_id, data, update_id=2, query_id="q1"):
    return {
        "update_id": update_id,
        "callback_query": {"id": query_id, "from": {"id": chat_id},
                           "message": {"chat": {"id": chat_id}},
                           "data": data},
    }


def buttons_of(calls):
    """Inline-keyboard buttons of the last sendMessage call."""
    sends = [p for m, p in calls if m == "sendMessage"]
    if not sends:
        return []
    kb = sends[-1].get("reply_markup", {}).get("inline_keyboard", [])
    return [b for row in kb for b in row]


def texts_of(calls):
    return [p["text"] for m, p in calls if m == "sendMessage"]


def make_calls(bot):
    calls = []

    def fake_tg(method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {}}

    bot.tg = fake_tg
    return calls


BASE_ENV = {"BOT_TOKEN": "test-token", "MIN_BNB_USD": "41",
            "TASK_CHANNELS": "", "CHANNEL_INVITE_URL":
            "https://t.me/+s2WrevCJwkFkOTVl",
            "OWNER_CONTACT": "@imzaman7",
            "ADMIN_CONTACT": "@RIYAD_KING_71",
            "REWARD_TEXT": "$3 per account"}

# --- A/B: health + /start seven buttons ----------------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()

check("health: /health is 200", client.get("/health").status_code == 200)
resp = client.post("/webhook", json=start_update())
check("start: /webhook /start returns 200", resp.status_code == 200)
btns = buttons_of(calls)
by_data = {b.get("callback_data"): b for b in btns if "callback_data" in b}
for data in ("connect_wallet", "check_status", "mint_10", "mint_5",
             "claim_3", "help", "disconnect"):
    check("start: {} button present".format(data), data in by_data, str(btns))
check("start: Mint Ten Share label",
      by_data.get("mint_10", {}).get("text") == bot._bold("💰 Mint Ten Share"))
check("start: Mint Five Share label",
      by_data.get("mint_5", {}).get("text") == bot._bold("💎 Mint Five Share"))
check("start: Claim $3 label (not $3.5)",
      by_data.get("claim_3", {}).get("text") == bot._bold("🎁 Claim $3"))
check("start: Claim button shows gift emoji in its text",
      by_data.get("claim_3", {}).get("text", "").startswith("🎁"))
check("start: every main button shows an emoji in its text",
      all(b.get("text", "")[:1] not in ("",) and not b["text"][0].isalnum()
          for b in btns), str(btns))
check("start: no Web App buttons anywhere",
      not any("web_app" in b for b in btns), str(btns))
check("start: all button labels are bold (after the leading emoji)",
      all(b["text"] and all(
          c == " " or c == "$" or "\U0001D400" <= c <= "\U0001D433"
          or "\U0001D7CE" <= c <= "\U0001D7D7"
          for c in b["text"][1:]) for b in btns), str(btns))
check("start: Join Channel label + shield emoji in text",
      any(b.get("text") == bot._bold("🛡 Join Channel")
          for b in btns))
check("start: Join Channel button with invite URL",
      any(b.get("url") == "https://t.me/+s2WrevCJwkFkOTVl" for b in btns))
check("start: Check Balance callback button present",
      "check_balance" in by_data)
check("start: Tasks callback button present", "tasks" in by_data)
texts = texts_of(calls)
check("start: welcome mentions Connect Wallet + $3",
      any("Connect Wallet" in t and "$3" in t for t in texts), str(texts))
check("start: welcome shows Owner @imzaman7",
      any("Owner: @imzaman7" in t for t in texts), str(texts))

# --- C: /mint route gone, no Web App references ---------------------------
check("mint route: GET /mint returns 404",
      client.get("/mint?qty=5").status_code == 404)
check("mint route: GET /mint?qty=10 returns 404",
      client.get("/mint?qty=10").status_code == 404)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
check("no webapp: bot.py has no 'web_app'",
      "web_app" not in src)
check("no webapp: bot.py has no 'WalletConnect'",
      "WalletConnect" not in src)
check("no webapp: bot.py has no 'mint_page'",
      "mint_page" not in src)
check("no webapp: bot.py has no 'WC_PROJECT_ID'",
      "WC_PROJECT_ID" not in src)
check("no webapp: bot.py has no '/mint' route",
      '"/mint"' not in src)
check("no webapp: mint_page.py file is deleted",
      not os.path.exists(os.path.join(
          os.path.dirname(os.path.abspath(__file__)), "mint_page.py")))

# --- D: Connect Wallet flow (mocked baw) ---------------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
photos = []
bot.send_photo = lambda cid, data, caption=None, reply_markup=None: \
    photos.append((cid, data, caption)) or {"ok": True}

FAKE_QR = {"qr_code_id": "qr-123", "qr_url": "https://app.binance.com/uni-qr/XYZ",
           "pairing_code": "ab12", "expires_at_ms": int(time.time() * 1000) + 300000}
signin_calls = []
baw_users.signin = lambda uid: signin_calls.append(uid) or dict(FAKE_QR)
baw_users.make_qr_png = lambda text: b"\x89PNGFAKE"

client.post("/webhook", json=callback_update(111, "connect_wallet"))
texts = texts_of(calls)
check("connect: generating message sent",
      any("QR বানানো হচ্ছে" in t for t in texts), str(texts))
check("connect: QR photo sent with PNG bytes",
      len(photos) == 1 and photos[0][1].startswith(b"\x89PNG"),
      str(photos))
check("connect: photo caption shows pairing code",
      photos and "ab12" in (photos[0][2] or ""), str(photos))
check("connect: signin called for user 111",
      signin_calls == [111], str(signin_calls))
rec = baw_users.get_record(111)
check("connect: pending QR stored in state",
      rec.get("pending_qr", {}).get("qr_code_id") == "qr-123", str(rec))

# real QR generation works (PNG magic) — reload to drop the mock
importlib.reload(baw_users)
check("connect: real make_qr_png returns a PNG",
      baw_users.make_qr_png("https://example.com").startswith(b"\x89PNG"))

# connect again while a live QR is pending -> resends, no new signin
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
photos.clear()
bot.send_photo = lambda cid, data, caption=None, reply_markup=None: \
    photos.append((cid, data, caption)) or {"ok": True}
baw_users.signin = lambda uid: signin_calls.append(uid) or dict(FAKE_QR)
baw_users.make_qr_png = lambda text: b"\x89PNGFAKE"
baw_users.save_record(111, {"pending_qr": {
    "qr_code_id": "qr-123", "qr_url": FAKE_QR["qr_url"],
    "pairing_code": "ab12",
    "expires_at_ms": int(time.time() * 1000) + 300000}})
n_before = len(signin_calls)
client.post("/webhook", json=callback_update(111, "connect_wallet"))
check("connect: live pending QR is resent, no new signin",
      len(signin_calls) == n_before and len(photos) == 1,
      str((signin_calls, len(photos))))

# connect when already paired -> no new QR
baw_users.save_record(111, {
    "paired_address": "0xE012133823df5452de9c10a73fE935d26B995A97"})
photos.clear()
client.post("/webhook", json=callback_update(111, "connect_wallet"))
check("connect: already paired -> no new QR, says so",
      len(photos) == 0 and any("already connected" in t for t in texts_of(calls)))

# --- E: Check status ------------------------------------------------------
USER_ADDR = "0x1111111111111111111111111111111111111111"

bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
photos.clear()
bot.send_photo = lambda cid, data, caption=None, reply_markup=None: \
    photos.append((cid, data, caption)) or {"ok": True}

# E1: pending QR, verify succeeds -> paired
baw_users.save_record(111, {"pending_qr": {
    "qr_code_id": "qr-123", "qr_url": FAKE_QR["qr_url"],
    "pairing_code": "ab12",
    "expires_at_ms": int(time.time() * 1000) + 300000}})
baw_users.verify = lambda uid, qid: {"ok": True}
baw_users.get_address = lambda uid: USER_ADDR
baw_users.get_agent_id = lambda uid: (True, "testuser.ai")
baw_users.eth_get_balance = lambda addr: 10 ** 17  # 0.1 BNB
baw_users.paid_once = lambda addr: False
client.post("/webhook", json=callback_update(111, "check_status"))
texts = texts_of(calls)
check("status: verify success -> Wallet connected!",
      any("Wallet connected!" in t for t in texts), str(texts))
rec = baw_users.get_record(111)
check("status: paired address stored, pending cleared",
      rec.get("paired_address") == USER_ADDR
      and "pending_qr" not in rec, str(rec))
check("status: shows Agent ID",
      any("testuser.ai" in t for t in texts), str(texts))

# E2: pending QR expired -> Bengali expiry message
baw_users.save_record(222, {"pending_qr": {
    "qr_code_id": "qr-old", "qr_url": FAKE_QR["qr_url"],
    "pairing_code": "ab12",
    "expires_at_ms": int(time.time() * 1000) - 1000}})
calls.clear()
client.post("/webhook", json=callback_update(222, "check_status"))
texts = texts_of(calls)
check("status: expired QR -> Bengali expiry message",
      any("মেয়াদ শেষ" in t and "Connect Wallet" in t for t in texts),
      str(texts))
check("status: expired pending cleared",
      "pending_qr" not in baw_users.get_record(222))

# E3: pending QR, not scanned yet -> "Not paired yet", pending kept
baw_users.save_record(333, {"pending_qr": {
    "qr_code_id": "qr-123", "qr_url": FAKE_QR["qr_url"],
    "pairing_code": "ab12",
    "expires_at_ms": int(time.time() * 1000) + 300000}})


def _verify_fail(uid, qid):
    raise baw_users.BawError("QR code not scanned")


baw_users.verify = _verify_fail
calls.clear()
client.post("/webhook", json=callback_update(333, "check_status"))
texts = texts_of(calls)
check("status: not scanned yet -> tells user to scan first",
      any("Not paired yet" in t for t in texts), str(texts))
check("status: pending kept when not scanned",
      "pending_qr" in baw_users.get_record(333))

# E4: no wallet at all -> press Connect Wallet
calls.clear()
client.post("/webhook", json=callback_update(444, "check_status"))
check("status: nothing paired -> says to connect",
      any("No wallet connected yet" in t for t in texts_of(calls)))

# --- F: per-user session isolation ----------------------------------------
bot, baw_users = load_bot(BASE_ENV)
homes_seen = []


def _recording_run_baw(home, args, timeout=120):
    homes_seen.append((home, list(args)))
    if args[:2] == ["auth", "signin"]:
        return {"qrCodeId": "qr-x", "urlForWeb": "https://app.binance.com/uni-qr/X",
                "pairingCode": "zz99", "expireAt": str(int(time.time() * 1000) + 300000)}
    raise AssertionError("unexpected baw call: {}".format(args))


baw_users.run_baw = _recording_run_baw
baw_users.signin(111)
baw_users.signin(222)
check("isolation: user 111 and 222 get different baw HOMEs",
      homes_seen[0][0] != homes_seen[1][0]
      and homes_seen[0][0].endswith("/111")
      and homes_seen[1][0].endswith("/222"), str(homes_seen))

# --- G: mint quote + confirm (mocked chain + baw) -------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
COST_10 = 78 * 10 ** 15  # 0.078 BNB
COST_5 = 39 * 10 ** 15
AI_BAL = {"v": 0}


def _cost(qty):
    return COST_10 if qty == 10 else COST_5


baw_users.cost_of = _cost
baw_users.total_minted = lambda: 206604
baw_users.eth_get_balance = lambda addr: 10 ** 18  # 1 BNB
baw_users.token_balance = lambda token, addr: AI_BAL["v"]
baw_users.estimate_ai_received = lambda cost, qty: 11380000000000 if qty == 10 else 5690000000000
baw_users.get_amounts_out = lambda amt, path: [amt, 76900000000000000 if amt > 10 ** 13 else 38450000000000000]
baw_users.dev_mode_enabled = lambda uid: True
bot.bnb_price_usd = lambda: 600.0
baw_users.paid_once = lambda addr: False
baw_users.estimate_mint_gas_wei = lambda: 20 * 10 ** 12  # 0.00002 BNB gas

# Referral: every mint from this bot must use W1 (the owner) as referrer.
_W1_HEX = "e012133823df5452de9c10a73fe935d26b995a97"
_calldata = baw_users.mint_calldata(10, COST_10, 1000, 2000)
check("referral: mint calldata uses W1 as referrer",
      baw_users.REFERRER_W1.lower() == "0x" + _W1_HEX
      and _W1_HEX in _calldata.lower(), str(_calldata)[:120])

# G1: quote when not paired -> connect first
calls.clear()
client.post("/webhook", json=callback_update(111, "mint_10"))
check("mint: not paired -> connect first",
      any("No wallet connected" in t for t in texts_of(calls)))

# G2: quote shows live numbers + Confirm button
baw_users.save_record(111, {"paired_address": USER_ADDR})
calls.clear()
client.post("/webhook", json=callback_update(111, "mint_10"))
texts = texts_of(calls)
check("mint: quote shows cost 0.07800 BNB",
      any("0.07800 BNB" in t for t in texts), str(texts))
check("mint: quote shows estimated gas + total needed",
      any("Gas: ~0.00002 BNB" in t
          and "মোট লাগবে: 0.07802 BNB" in t for t in texts),
      str(texts))
check("mint: quote shows expected AI + sell value",
      any("11,380,000,000,000" in t and "0.07690" in t for t in texts),
      str(texts))
check("mint: quote says sell is NOT a profit",
      any("profit হবে না" in t for t in texts), str(texts))
btns = buttons_of(calls)
check("mint: Confirm Mint button present",
      any(b.get("callback_data") == "confirm_mint_10" for b in btns),
      str(btns))
rec = baw_users.get_record(111)
check("mint: pending_mint stored with cost + expected start",
      rec.get("pending_mint", {}).get("qty") == 10
      and rec["pending_mint"]["cost_wei"] == COST_10
      and rec["pending_mint"]["expected_start"] == 206605, str(rec))

# G3: quote above the cap -> stopped
baw_users.cost_of = lambda qty: 90 * 10 ** 15  # 0.09 BNB for 10
calls.clear()
client.post("/webhook", json=callback_update(111, "mint_10"))
check("mint: cost above cap -> stopped",
      any("above the 0.008 BNB/share cap" in t for t in texts_of(calls)))
baw_users.cost_of = _cost

# G4: insufficient BNB -> top up message
baw_users.eth_get_balance = lambda addr: 10 ** 15  # 0.001 BNB
calls.clear()
client.post("/webhook", json=callback_update(111, "mint_10"))
check("mint: insufficient BNB -> top up message",
      any("BNB কম আছে" in t and "লাগবে:" in t
          and "আর লাগবে:" in t for t in texts_of(calls)))
baw_users.eth_get_balance = lambda addr: 10 ** 18

# G5: Developer Mode off -> instructions, no quote
baw_users.dev_mode_enabled = lambda uid: False
calls.clear()
client.post("/webhook", json=callback_update(111, "mint_10"))
check("mint: dev mode off -> enable instructions",
      any("Developer Mode is OFF" in t for t in texts_of(calls)))
baw_users.dev_mode_enabled = lambda uid: True

# G6: confirm -> mint + sell, tx hashes, minted_count up
do_mint_calls = []
baw_users.do_mint = lambda home, addr, qty, cost, start: \
    do_mint_calls.append((home, addr, qty, cost, start)) or \
    {"tx_hash": "0xminttxhash", "status": ""}
sell_calls = []
baw_users.sell_ai = lambda home, addr, amt: \
    sell_calls.append((home, addr, amt)) or \
    {"approve_tx": "0xapprovetx", "swap_tx": "0xswaptx",
     "expected_bnb_wei": 76900000000000000}
# token_balance: before mint 0, after mint the received AI
_bal_state = {"n": 0}


def _bal_seq(token, addr):
    _bal_state["n"] += 1
    return 0 if _bal_state["n"] == 1 else 11380000000000


baw_users.token_balance = _bal_seq
# eth_get_balance: before mint 1 BNB, after sell ~0.9985 BNB
_bnb_state = {"n": 0}


def _bnb_seq(addr):
    _bnb_state["n"] += 1
    return 10 ** 18 if _bnb_state["n"] <= 1 else 998500000000000000


baw_users.eth_get_balance = _bnb_seq
calls.clear()
client.post("/webhook", json=callback_update(111, "confirm_mint_10"))
texts = texts_of(calls)
check("mint: confirm -> Mint সফল with shares + total",
      any("Mint সফল" in t and "Shares: 10 (মোট আপনার: 10)" in t
          for t in texts), str(texts))
check("mint: confirm -> mint tx hash shown",
      any("0xminttxhash" in t for t in texts), str(texts))
check("mint: confirm -> sell reported with swap tx",
      any("0xswaptx" in t for t in texts), str(texts))
check("mint: do_mint used the user's HOME + address + live cost",
      do_mint_calls and do_mint_calls[0][1] == USER_ADDR
      and do_mint_calls[0][2] == 10 and do_mint_calls[0][3] == COST_10,
      str(do_mint_calls))
check("mint: sell got exactly the AI received",
      sell_calls and sell_calls[0][2] == 11380000000000, str(sell_calls))
rec = baw_users.get_record(111)
check("mint: minted_count=1, pending cleared, tx stored",
      rec.get("minted_count") == 1 and "pending_mint" not in rec
      and rec.get("last_mint_tx") == "0xminttxhash", str(rec))

# G7: confirm with expired quote -> re-quote, no mint
baw_users.save_record(111, {"paired_address": USER_ADDR,
                            "pending_mint": {"qty": 10, "cost_wei": COST_10,
                                             "expected_start": 206605,
                                             "ai_before_wei": 0,
                                             "quoted_at": int(time.time()) - 300}})
do_mint_calls.clear()
calls.clear()
client.post("/webhook", json=callback_update(111, "confirm_mint_10"))
check("mint: expired quote -> re-quote, no mint sent",
      not do_mint_calls and any("quote expired" in t for t in texts_of(calls)))

# G8: confirm when preview/execute fails -> Mint failed, nothing counted
baw_users.save_record(111, {"paired_address": USER_ADDR, "minted_count": 1,
                            "pending_mint": {"qty": 5, "cost_wei": COST_5,
                                             "expected_start": 206605,
                                             "ai_before_wei": 0,
                                             "quoted_at": int(time.time())}})


def _do_mint_fail(home, addr, qty, cost, start):
    raise baw_users.BawError("mint preview not clean (status: BLOCKED)")


baw_users.do_mint = _do_mint_fail
calls.clear()
client.post("/webhook", json=callback_update(111, "confirm_mint_5"))
texts = texts_of(calls)
check("mint: preview failure -> Mint failed, nothing sent",
      any("Mint failed" in t and "BLOCKED" in t for t in texts), str(texts))
check("mint: failed mint does not count",
      baw_users.get_record(111).get("minted_count") == 1)

# G9: cancel clears the pending mint
baw_users.save_record(111, {"paired_address": USER_ADDR,
                            "pending_mint": {"qty": 10, "cost_wei": COST_10,
                                             "expected_start": 206605,
                                             "ai_before_wei": 0,
                                             "quoted_at": int(time.time())}})
calls.clear()
client.post("/webhook", json=callback_update(111, "cancel_mint"))
check("mint: cancel clears pending + says nothing sent",
      "pending_mint" not in baw_users.get_record(111)
      and any("nothing was sent" in t.lower() for t in texts_of(calls)))

# --- H: Claim $3 -----------------------------------------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
baw_users.w1_usdt_balance_wei = lambda: 0
baw_users.pay_claim_usdt = lambda addr, amt: "0xclaimtx"

# H1: no mint yet -> needs a mint first
baw_users.save_record(111, {"paired_address": USER_ADDR})
calls.clear()
client.post("/webhook", json=callback_update(111, "claim_3"))
check("claim: no mint yet -> needs a mint first",
      any("needs a mint first" in t for t in texts_of(calls)))
check("claim: mystery-box sticker sent once on Claim press",
      [(m, p) for m, p in calls if m == "sendSticker"] == [
          ("sendSticker",
           {"chat_id": 111, "sticker": bot.BOX_STICKER_ID})],
      str(calls))

# H2: minted but W1 pool empty -> plain message, claim NOT used up
baw_users.save_record(111, {"paired_address": USER_ADDR, "minted_count": 1})
calls.clear()
client.post("/webhook", json=callback_update(111, "claim_3"))
texts = texts_of(calls)
check("claim: empty pool -> plain message + contact owner",
      any("reward pool is empty" in t for t in texts), str(texts))
check("claim: empty pool does NOT mark claimed",
      baw_users.get_record(111).get("claimed") is not True)

# H3: pool funded -> USDT sent, claimed marked, second claim refused
baw_users.w1_usdt_balance_wei = lambda: 10 * 10 ** 18  # $10 USDT
pay_calls = []
baw_users.pay_claim_usdt = lambda addr, amt: \
    pay_calls.append((addr, amt)) or "0xclaimtx"
calls.clear()
client.post("/webhook", json=callback_update(111, "claim_3"))
texts = texts_of(calls)
check("claim: success -> USDT sent with tx",
      any("USDT sent" in t and "0xclaimtx" in t for t in texts), str(texts))
check("claim: pays exactly $3 in USDT wei to the user",
      pay_calls == [(USER_ADDR, 3 * 10 ** 18)], str(pay_calls))
check("claim: marked claimed with tx",
      baw_users.get_record(111).get("claimed") is True
      and baw_users.get_record(111).get("claimed_tx") == "0xclaimtx")
calls.clear()
client.post("/webhook", json=callback_update(111, "claim_3"))
check("claim: second claim refused (one per account)",
      any("already claimed" in t for t in texts_of(calls)))

# H4: claim payment failure -> claim NOT used up
baw_users.save_record(222, {"paired_address": USER_ADDR, "minted_count": 1})


def _pay_fail(addr, amt):
    raise baw_users.BawError("claim preview not clean (status: BLOCKED)")


baw_users.pay_claim_usdt = _pay_fail
calls.clear()
client.post("/webhook", json=callback_update(222, "claim_3"))
texts = texts_of(calls)
check("claim: payment failure -> not used up",
      any("Claim failed" in t for t in texts)
      and baw_users.get_record(222).get("claimed") is not True,
      str(texts))

# --- I: Disconnect + Help --------------------------------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
signout_calls = []
baw_users.signout = lambda uid: signout_calls.append(uid)
baw_users.save_record(111, {"paired_address": USER_ADDR, "minted_count": 1})
calls.clear()
client.post("/webhook", json=callback_update(111, "disconnect"))
check("disconnect: signout called for the user",
      signout_calls == [111], str(signout_calls))
check("disconnect: state cleared + confirmed",
      baw_users.get_record(111) == {}
      and any("Disconnected" in t for t in texts_of(calls)))
calls.clear()
client.post("/webhook", json=callback_update(111, "disconnect"))
check("disconnect: nothing connected -> says so",
      any("No wallet is connected" in t for t in texts_of(calls)))
calls.clear()
client.post("/webhook", json=callback_update(111, "help"))
texts = texts_of(calls)
check("help: explains Connect Wallet + Developer Mode",
      any("Connect Wallet" in t and "Developer Mode" in t for t in texts),
      str(texts))
btns = buttons_of(calls)
check("help: Owner + Admin buttons open their Telegram chats",
      any(b.get("url") == "https://t.me/imzaman7" for b in btns)
      and any(b.get("url") == "https://t.me/RIYAD_KING_71"
              for b in btns),
      str(btns))

# --- J: balance + tasks (kept from before) ---------------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
client = bot.app.test_client()
bot.bnb_balance = lambda addr: 0.1
bot.bnb_price_usd = lambda: 500.0
baw_users.token_balance = lambda token, addr: 25 * 10 ** 18  # 25 USDT
client.post("/webhook", json=callback_update(111, "check_balance"))
client.post("/webhook", json={
    "update_id": 3,
    "message": {"message_id": 2, "chat": {"id": 111},
                "from": {"id": 111},
                "text": "0xE012133823df5452de9c10a73fE935d26B995A97"}})
texts = texts_of(calls)
check("balance: eligible at $50 vs $41 min",
      any("Eligible" in t and "$50.00" in t for t in texts), str(texts))
check("balance: shows BNB + USDT + BNB value in USDT",
      any("BNB: 0.10000" in t and "USDT: 25.00" in t
          and "BNB value: $50.00" in t for t in texts), str(texts))
calls.clear()
client.post("/webhook", json=callback_update(111, "tasks"))
check("tasks: no channels -> optional, can mint now",
      any("optional" in t and "You can mint now" in t for t in texts_of(calls)))

# --- K: polling mode dispatches a /start update -------------------------
bot, baw_users = load_bot(BASE_ENV)
calls = make_calls(bot)
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
texts = texts_of(calls)
check("polling: /start update answered with welcome",
      any("Welcome" in t and "Owner: @imzaman7" in t for t in texts),
      str(texts))
check("polling: stale webhook cleared first",
      calls and calls[0][0] == "deleteWebhook", str(calls[:1]))

print()
if FAILURES:
    print("FAILED: {}".format(", ".join(FAILURES)))
    sys.exit(1)
print("ALL TESTS PASSED")
