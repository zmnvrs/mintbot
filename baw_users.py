"""Per-user Binance Agentic Wallet (baw) sessions for the mint bot.

Every Telegram user gets an ISOLATED baw session: a separate HOME
directory per Telegram user id (``~/.baw-users/<telegram_user_id>/``).
All baw invocations for a user run with that user's HOME, so sessions
can never leak between users. The bot only ever holds a qrCodeId and
session state — never a private key or secret (baw handles auth).

Flows implemented here (all read-only unless noted):
  * signin  — ``baw auth signin --json`` -> QR URL + qrCodeId (~5 min
    validity). The QR encodes the Binance pairing URL; the user scans
    it in the Binance app.
  * verify  — ``baw auth verify --qrCodeId <id>`` after the scan.
  * status / address / agent-id / settings — read-only wallet state.
  * mint    — live chain reads (costOf, totalMinted, paidOnce, pair
    reserves, router quote) + ``baw contract-call preview`` (clean
    preview required) + ``baw contract-call execute``.
  * sell    — after a mint, approve the PancakeSwap router for exactly
    the AI received and swap it for BNB (instant reward).
  * claim   — pay CLAIM_USD in USDT from the owner's W1 session to a
    user, via a USDT contract-call transfer (not ``wallet send``, which
    is blocked unless the recipient is in W1's address book).

The owner's W1 session lives at W1_BAW_HOME (default /home/hatch) and
is used ONLY to pay claims and to read referral state. It is never
used to mint through this bot.

No function here ever pairs or mints a real wallet on its own — every
state-changing call is driven by an explicit user button tap in bot.py.
Tests mock ``run_baw`` / the chain reads, never a live session.
"""

import io
import json
import os
import shutil
import subprocess
import time

import requests
import qrcode
from eth_abi import decode, encode
from eth_utils import keccak

# ------------------------------------------------------------ chain facts
# Verified on-chain (BNB Smart Chain, chainId 56). The mint contract is
# live; the old agentidai.meme site is dead and is not used anywhere.
CHAIN_ID = 56
MINT_CONTRACT = "0xa4bFa31f615F8e2a13dce3d93C0996CC5a56763D"
AI_TOKEN = "0x2a60AaB19b182B5C4b47fffC066ADA06Ab27924f"
WBNB_TOKEN = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
AI_PAIR = "0x6c3d8b927B8c38d5907aE64b17804871c97CEf97"
USDT_TOKEN = "0x55d398326f99059fF775485246999027B3197955"
# Referrer for every mint from this bot: W1, the owner's wallet. The
# referral bonus goes to W1. Never use any other referrer.
REFERRER_W1 = "0xE012133823df5452de9c10a73fE935d26B995A97"
W1_ADDRESS = REFERRER_W1
# Hard cap on the mint price: 0.008 BNB per share (0.04 for 5, 0.08
# for 10). If costOf(qty) is above the cap, stop — do not mint.
MAX_COST_PER_SHARE_WEI = 8 * 10 ** 15
# AI the minter gets per share on top of the BNB->AI conversion.
USER_SHARE_PER_SHARE = 50_000_000
# Extra BNB kept aside for gas when checking affordability.
GAS_RESERVE_WEI = 10 ** 14  # 0.0001 BNB
# Typical gas for one Agent ID mint call (the AGENTRA mint used ~169k;
# 250k leaves margin for gas-price spikes). Used to estimate the mint's
# gas cost in the quote: gas_price x MINT_GAS_UNITS.
MINT_GAS_UNITS = 250000

BSC_RPC = os.environ.get(
    "BSC_RPC", "https://bsc-dataseed.bnbchain.org").strip()
# Fallback BSC RPC endpoints, tried in order when the primary fails.
# The primary dataseed returns 403 (HTML, not JSON) at times — a single
# endpoint means every chain read (balance, mint cost, gas) fails at
# once, so the bot must rotate to a working one.
BSC_RPC_FALLBACKS = [
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-dataseed2.bnbchain.org",
    "https://bsc-dataseed4.bnbchain.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc.publicnode.com",
    "https://1rpc.io/bnb",
]
USDT_DECIMALS = 18


class BawError(Exception):
    """A baw CLI call failed, or a chain read failed."""


# ------------------------------------------------------- session handling

def users_root():
    """Root dir holding one baw HOME per Telegram user id."""
    return os.path.expanduser(
        os.environ.get("BAW_USERS_HOME", "~/.baw-users"))


def user_home(user_id):
    """Isolated baw HOME for one Telegram user id."""
    return os.path.join(users_root(), str(user_id))


def w1_home():
    """The owner's W1 baw session HOME (claims only, never mints)."""
    return os.path.expanduser(os.environ.get("W1_BAW_HOME", "/home/hatch"))


def state_path():
    return os.environ.get(
        "BAW_STATE_FILE", os.path.join(users_root(), "state.json"))


def load_state():
    """Per-user bot records: {str(user_id): record dict}. {} if none."""
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    """Atomically write the per-user records (tmp file + replace)."""
    path = state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "{}.tmp-{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True)
    os.replace(tmp, path)


def get_record(user_id):
    return load_state().get(str(user_id), {})


def save_record(user_id, record):
    state = load_state()
    state[str(user_id)] = record
    save_state(state)


def baw_bin():
    return os.environ.get("BAW_BIN") or shutil.which("baw") or "baw"


def run_baw(home, args, timeout=120):
    """Run one baw command with HOME set to a user's session dir.

    Returns the ``data`` payload of the --json output. Raises BawError
    on any failure (not installed, not logged in, backend error,
    timeout, non-JSON output). Never logs or returns a secret.
    """
    env = dict(os.environ)
    env["HOME"] = home
    os.makedirs(home, exist_ok=True)
    cmd = [baw_bin()] + list(args) + ["--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        raise BawError(
            "baw CLI is not installed on this host (run: npm install -g "
            "@binance/agentic-wallet@1.12.0)")
    except subprocess.TimeoutExpired:
        raise BawError("baw timed out after {}s".format(timeout))
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except ValueError:
        raise BawError("baw returned non-JSON output: {}".format(
            (out or err)[:300]))
    if isinstance(payload, dict) and payload.get("success") is False:
        error = payload.get("error") or {}
        raise BawError("baw error {}: {}".format(
            error.get("code"), error.get("message") or error))
    if proc.returncode != 0:
        raise BawError("baw exited {}: {}".format(
            proc.returncode, (out or err)[:300]))
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ------------------------------------------------------------- QR pairing

def signin(user_id):
    """Start the Binance sign-in flow for one user.

    Returns {"qr_code_id", "qr_url", "pairing_code", "expires_at_ms"}.
    The QR encodes qr_url; the user scans it in the Binance app.
    Nothing is authenticated until the user scans and verify() runs.
    """
    data = run_baw(user_home(user_id), ["auth", "signin"], timeout=60)
    try:
        return {
            "qr_code_id": data["qrCodeId"],
            "qr_url": data["urlForWeb"],
            "pairing_code": data.get("pairingCode", ""),
            "expires_at_ms": int(data["expireAt"]),
        }
    except (KeyError, TypeError, ValueError):
        raise BawError("unexpected signin response: {}".format(str(data)[:300]))


def make_qr_png(text):
    """PNG bytes of a QR code for the given text (Binance pairing URL)."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def verify(user_id, qr_code_id):
    """Verify a QR scan (polls until wallet creation completes).

    Returns the verify payload. Raises BawError if the QR was not
    scanned, expired, or the backend failed. Callers must confirm the
    session with wallet_status() afterwards.
    """
    return run_baw(user_home(user_id),
                   ["auth", "verify", "--qrCodeId", qr_code_id],
                   timeout=180)


def signout(user_id):
    """Unpair one user's wallet and drop their local session dir."""
    try:
        run_baw(user_home(user_id), ["auth", "signout"], timeout=60)
    except BawError:
        pass  # already signed out / no session — still clear local state
    shutil.rmtree(user_home(user_id), ignore_errors=True)


# --------------------------------------------------------- wallet reading

def wallet_status(user_id):
    """Raw status payload, e.g. {"status": "UNCONNECTED"} when signed out."""
    return run_baw(user_home(user_id), ["wallet", "status"], timeout=60)


def _find_evm_address(obj):
    """Recursively find the first 0x + 40 hex string in parsed JSON."""
    if isinstance(obj, str):
        s = obj.strip()
        if len(s) == 42 and s.startswith("0x"):
            try:
                int(s, 16)
                return s
            except ValueError:
                return None
        return None
    if isinstance(obj, dict):
        for key in ("evm", "EVM", "address", "walletAddress", "bsc"):
            if key in obj:
                found = _find_evm_address(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = _find_evm_address(value)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _find_evm_address(item)
            if found:
                return found
    return None


def get_address(user_id):
    """The user's paired EVM (BSC) address, or None if not paired."""
    try:
        data = run_baw(user_home(user_id), ["wallet", "address"],
                       timeout=60)
    except BawError:
        return None
    return _find_evm_address(data)


def get_agent_id(user_id):
    """(claimed_bool, agent_id_or_None) for the paired wallet."""
    try:
        data = run_baw(user_home(user_id), ["wallet", "agent-id"],
                       timeout=60)
    except BawError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    claimed = data.get("claimed")
    agent = (data.get("agentId") or data.get("agent_id")
             or data.get("domain") or data.get("name"))
    if isinstance(agent, str) and not agent.endswith(".ai"):
        agent = None
    if claimed is None:
        claimed = bool(agent)
    return bool(claimed), agent


def dev_mode_enabled(user_id):
    """True/False if the wallet settings say so, None if unknown.

    Developer Mode must be ON in the Binance app (Agentic Wallet
    settings) for custom contract calls (mint) to work.
    """
    try:
        data = run_baw(user_home(user_id), ["wallet", "settings"],
                       timeout=60)
    except BawError:
        return None
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "devMode" and isinstance(value, dict):
                    found["enabled"] = value.get("enabled")
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return found.get("enabled")


# ------------------------------------------------------------ chain reads

def _selector(sig):
    return keccak(text=sig)[:4].hex()


def _calldata(sig, types, values):
    return "0x" + _selector(sig) + encode(types, values).hex()


def _rpc(method, params):
    """One JSON-RPC call to BSC, rotating endpoints on failure.

    Tries the primary BSC_RPC first, then each fallback. An endpoint
    that returns non-JSON (e.g. a 403 HTML page), an RPC error, or a
    timeout is skipped and the next one is tried. Returns the result
    field of the first good response. Raises BawError only when every
    endpoint failed.
    """
    endpoints = [BSC_RPC] + [u for u in BSC_RPC_FALLBACKS if u != BSC_RPC]
    last_err = "no endpoint tried"
    for url in endpoints:
        try:
            resp = requests.post(
                url,
                json={"jsonrpc": "2.0", "method": method,
                      "params": params, "id": 1},
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"})
            res = resp.json()
        except Exception as exc:
            last_err = "{}: {}".format(url, exc)
            continue
        if "error" in res:
            last_err = "{}: {}".format(url, res["error"])
            continue
        if "result" not in res:
            last_err = "{}: no result in response".format(url)
            continue
        return res["result"]
    raise BawError("BSC RPC unreachable: {}".format(last_err))


def eth_call(to, data):
    """Raw eth_call on BSC. Returns the 0x result, raises BawError."""
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"]) or "0x"


def eth_get_balance(address):
    return int(_rpc("eth_getBalance", [address, "latest"]), 16)


def gas_price_wei():
    """Live BSC gas price in wei (eth_gasPrice)."""
    return int(_rpc("eth_gasPrice", []), 16)


def estimate_mint_gas_wei():
    """Estimated gas cost of one mint in wei (live gas price x typical
    gas units). An estimate — the exact gas is set by the network."""
    return gas_price_wei() * MINT_GAS_UNITS


def cost_of(qty):
    raw = eth_call(MINT_CONTRACT,
                   _calldata("costOf(uint256)", ["uint256"], [qty]))
    return decode(["uint256"], bytes.fromhex(raw[2:]))[0]


def total_minted():
    raw = eth_call(MINT_CONTRACT,
                   _calldata("totalMinted()", [], []))
    return decode(["uint256"], bytes.fromhex(raw[2:]))[0]


def paid_once(address):
    raw = eth_call(MINT_CONTRACT,
                   _calldata("paidOnce(address)", ["address"], [address]))
    return decode(["bool"], bytes.fromhex(raw[2:]))[0]


def token_balance(token, address):
    raw = eth_call(token,
                   _calldata("balanceOf(address)", ["address"], [address]))
    return decode(["uint256"], bytes.fromhex(raw[2:]))[0]


def pair_reserves():
    """(reserve AI, reserve WBNB) of the AI/WBNB pair (token0=AI)."""
    raw = eth_call(AI_PAIR, _calldata("getReserves()", [], []))
    r0, r1, _ = decode(["uint112", "uint112", "uint32"],
                       bytes.fromhex(raw[2:]))
    return r0, r1


def get_amounts_out(amount_in_wei, path):
    """Router quote: [amount_in, amount_out] for path (e.g. AI->WBNB)."""
    raw = eth_call(
        PANCAKE_ROUTER,
        _calldata("getAmountsOut(uint256,address[])",
                  ["uint256", "address[]"], [amount_in_wei, path]))
    return decode(["uint256[]"], bytes.fromhex(raw[2:]))[0]


def estimate_ai_received(cost_wei, qty):
    """Estimate the AI a mint returns (pool price + USER_SHARE).

    The minter gets the BNB paid converted to AI at the live pool
    price, plus USER_SHARE per share. Labelled an estimate — the exact
    amount is the wallet balance change after the mint.
    """
    reserve_ai, reserve_bnb = pair_reserves()
    if reserve_bnb == 0:
        return USER_SHARE_PER_SHARE * qty
    converted = cost_wei * reserve_ai // reserve_bnb
    return converted + USER_SHARE_PER_SHARE * qty


# ------------------------------------------------------- contract calls

def preview(home, from_addr, to, value_wei, input_data, gas_limit=None):
    """Preview a contract call. Returns {"request_id", "status", "raw"}.

    A clean preview has a requestId and no blocked/failed status.
    A PENDING_CONFIRMATION status means the user must confirm in the
    Binance app before any txHash exists — callers must report that
    instead of treating it as a completed mint.
    """
    args = ["contract-call", "preview", "--binanceChainId", str(CHAIN_ID),
            "--from", from_addr, "--to", to, "--value", str(value_wei),
            "--inputData", input_data]
    if gas_limit:
        args += ["--gasLimit", str(gas_limit)]
    data = run_baw(home, args, timeout=120)
    if not isinstance(data, dict):
        raise BawError("unexpected preview response: {}".format(str(data)[:200]))
    request_id = (data.get("requestId") or data.get("request_id")
                  or data.get("id"))
    status = str(data.get("status") or "")
    return {"request_id": request_id, "status": status, "raw": data}


def preview_is_clean(preview_result):
    """True only when the preview can be executed right away."""
    if not preview_result.get("request_id"):
        return False
    status = (preview_result.get("status") or "").upper()
    if any(word in status for word in
           ("BLOCK", "REJECT", "FAIL", "HIGH_RISK", "RISK")):
        return False
    return True


def execute(home, request_id):
    """Execute a previewed call. Returns {"tx_hash", "status", "raw"}."""
    data = run_baw(home, ["contract-call", "execute",
                          "--requestId", request_id], timeout=180)
    if not isinstance(data, dict):
        raise BawError("unexpected execute response: {}".format(str(data)[:200]))
    tx_hash = (data.get("txHash") or data.get("transactionHash")
               or data.get("hash") or data.get("tx_hash"))
    return {"tx_hash": tx_hash, "status": str(data.get("status") or ""),
            "raw": data}


def mint_calldata(qty, max_cost_wei, expected_start, deadline):
    return _calldata(
        "mint(uint256,address,uint256,uint256,uint256,uint256)",
        ["uint256", "address", "uint256", "uint256", "uint256", "uint256"],
        [qty, REFERRER_W1, max_cost_wei, expected_start, 0, deadline])


def do_mint(home, from_addr, qty, max_cost_wei, expected_start):
    """Preview + execute one mint. Returns {"tx_hash", "status"}.

    Retries the preview once with an explicit gas limit if the first
    preview fails (baw gas estimation can under-estimate).
    Raises BawError on any failure — callers report it plainly.
    """
    deadline = int(time.time()) + 600
    data = mint_calldata(qty, max_cost_wei, expected_start, deadline)
    try:
        prev = preview(home, from_addr, MINT_CONTRACT, max_cost_wei, data)
    except BawError:
        prev = preview(home, from_addr, MINT_CONTRACT, max_cost_wei, data,
                       gas_limit=400000)
    if not preview_is_clean(prev):
        raise BawError("mint preview not clean (status: {}). "
                       "Not minting.".format(prev.get("status") or "unknown"))
    result = execute(home, prev["request_id"])
    if not result.get("tx_hash"):
        raise BawError(
            "mint execute returned no tx hash (status: {}). The mint may "
            "need confirmation in the Binance app — check status and "
            "try again.".format(result.get("status") or "unknown"))
    return result


def sell_ai(home, from_addr, ai_amount_wei):
    """Sell exactly ai_amount_wei AI for BNB via PancakeSwap.

    Approves the router for exactly that amount, then swaps with a 5%
    slippage guard. Returns {"approve_tx", "swap_tx",
    "expected_bnb_wei"}. Raises BawError on any failure.
    """
    if ai_amount_wei <= 0:
        raise BawError("nothing to sell (0 AI received)")
    amounts = get_amounts_out(ai_amount_wei, [AI_TOKEN, WBNB_TOKEN])
    expected_bnb = amounts[1]
    min_out = expected_bnb * 95 // 100
    approve_data = _calldata("approve(address,uint256)",
                             ["address", "uint256"],
                             [PANCAKE_ROUTER, ai_amount_wei])
    prev = preview(home, from_addr, AI_TOKEN, 0, approve_data)
    if not preview_is_clean(prev):
        raise BawError("sell approve preview not clean (status: {}). "
                       "AI stays in your wallet.".format(
                           prev.get("status") or "unknown"))
    approve_res = execute(home, prev["request_id"])
    if not approve_res.get("tx_hash"):
        raise BawError("sell approve returned no tx hash. "
                       "AI stays in your wallet.")
    deadline = int(time.time()) + 600
    swap_data = _calldata(
        "swapExactTokensForETHSupportingFeeOnTransferTokens"
        "(uint256,uint256,address[],address,uint256)",
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [ai_amount_wei, min_out, [AI_TOKEN, WBNB_TOKEN], from_addr,
         deadline])
    prev2 = preview(home, from_addr, PANCAKE_ROUTER, 0, swap_data)
    if not preview_is_clean(prev2):
        raise BawError("sell swap preview not clean (status: {}). "
                       "AI stays in your wallet.".format(
                           prev2.get("status") or "unknown"))
    swap_res = execute(home, prev2["request_id"])
    if not swap_res.get("tx_hash"):
        raise BawError("sell swap returned no tx hash. "
                       "AI stays in your wallet.")
    return {"approve_tx": approve_res["tx_hash"],
            "swap_tx": swap_res["tx_hash"],
            "expected_bnb_wei": expected_bnb}


# ------------------------------------------------------------------ claim

def w1_usdt_balance_wei():
    """W1's USDT balance (wei, 18 decimals) via a chain read."""
    return token_balance(USDT_TOKEN, W1_ADDRESS)


def pay_claim_usdt(user_address, amount_usdt_wei):
    """Pay a $CLAIM_USD reward in USDT from W1 to a user's wallet.

    Sent as a USDT contract-call transfer from the W1 baw session
    (not ``wallet send`` — that is blocked unless the recipient is in
    W1's address book). Returns the tx hash. Raises BawError on any
    failure; callers must NOT mark the claim as paid then.
    """
    data = _calldata("transfer(address,uint256)",
                     ["address", "uint256"],
                     [user_address, amount_usdt_wei])
    prev = preview(w1_home(), W1_ADDRESS, USDT_TOKEN, 0, data)
    if not preview_is_clean(prev):
        raise BawError("claim preview not clean (status: {}).".format(
            prev.get("status") or "unknown"))
    result = execute(w1_home(), prev["request_id"])
    if not result.get("tx_hash"):
        raise BawError("claim execute returned no tx hash (status: {})."
                       .format(result.get("status") or "unknown"))
    return result["tx_hash"]
