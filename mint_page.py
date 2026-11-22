"""Self-contained mint Web App page served by the bot at GET /mint.

The page runs in the user's browser (inside Telegram as a Mini App).
It connects the user's OWN EVM wallet via WalletConnect, reads the
live mint numbers from the Agent ID (AI) mint contract on BNB Smart
Chain, shows them to the user BEFORE they confirm, and only then
sends the mint. The bot itself never holds a wallet or signs anything.

Safety rules enforced by the page (all numbers are read live, nothing
is guessed):
  - Mint cost comes from costOf(qty) read right before sending; the
    transaction value is exactly that cost — BNB is never sent directly
    to the contract address, only as the mint call's value.
  - If costOf(qty) is above the per-share cap (0.008 BNB/share) the
    page stops and does not mint.
  - The minting wallet must not be the referrer (W1) itself.
  - The page simulates the mint (static call) right before sending;
    if the simulation reverts (e.g. no .ai Agent ID) it stops and
    shows the error.
  - The Mint button stays disabled until the live quote (cost, AI
    amount, expected sell value) has loaded and been shown.
  - The expected AI amount is ESTIMATED from the live pool price
    (mint cost x AI reserve / BNB reserve); the exact amount is
    confirmed from the wallet's AI balance after the mint. The
    expected sell value comes from the router's getAmountsOut. If the
    expected sell value is below the mint cost, the page warns the
    user plainly — selling now would lose money.
  - Instant sell is OFF by default (ENABLE_INSTANT_SELL). When it is
    on, the page sells ONLY the AI received from this mint (balance
    change), approves the router for exactly that amount, and swaps
    with a 5% slippage guard. The sell is never presented as a profit
    or a reward.

WalletConnect needs a project ID (WC_PROJECT_ID env var). When it is
unset the page shows a clear "wallet connect is not configured yet"
message and the Connect button stays disabled. No project ID or any
other credential is ever hardcoded here.
"""

import json

_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mint %%QTY%% Agent ID shares</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/ethers@6.13.0/dist/ethers.umd.min.js"></script>
<script src="https://unpkg.com/@walletconnect/ethereum-provider@2.17.1/dist/index.umd.js"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 16px;
         background: var(--tg-theme-bg-color, #fff);
         color: var(--tg-theme-text-color, #111); }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p { font-size: 14px; line-height: 1.5; }
  .card { border: 1px solid #ccc; border-radius: 10px; padding: 12px;
          margin: 12px 0; }
  .row { display: flex; justify-content: space-between; gap: 8px;
         font-size: 14px; padding: 4px 0; }
  .row b { text-align: right; }
  .warn { background: #fff3cd; border: 1px solid #e6a800; border-radius: 8px;
          padding: 10px; font-size: 14px; margin: 12px 0; }
  .error { background: #fde8e8; border: 1px solid #d33; border-radius: 8px;
           padding: 10px; font-size: 14px; margin: 12px 0; }
  .ok { background: #e6f4ea; border: 1px solid #2e7d32; border-radius: 8px;
        padding: 10px; font-size: 14px; margin: 12px 0; }
  button { width: 100%; padding: 14px; font-size: 16px; font-weight: bold;
           border: 0; border-radius: 10px; margin: 8px 0; cursor: pointer;
           background: #2481cc; color: #fff; }
  button:disabled { background: #999; cursor: not-allowed; }
  .mono { font-family: monospace; font-size: 12px; word-break: break-all; }
  .small { font-size: 12px; color: #666; }
</style>
</head>
<body>
<h1>Mint %%QTY%% Agent ID (AI) shares</h1>
<p>BNB Smart Chain. You connect <b>your own wallet</b> and mint from it.
The referral bonus goes to the owner's wallet (W1).</p>
<div class="warn"><b>Use a regular wallet</b> (MetaMask, Trust Wallet, or Binance Web3 Wallet) — do <b>NOT</b> connect a Binance Agentic Wallet (baw); that can disconnect its session. Your wallet must hold a .ai Agent ID and enough BNB for the mint cost plus gas.</div>
%%WC_WARNING%%
<div id="status" class="card">Loading&hellip;</div>

<button id="connectBtn" disabled>Connect Wallet</button>

<div id="quote" class="card" hidden>
  <div class="row"><span>Your wallet</span><b id="qWallet" class="mono">-</b></div>
  <div class="row"><span>Shares</span><b id="qShares">-</b></div>
  <div class="row"><span>Mint cost</span><b id="qCost">-</b></div>
  <div class="row"><span>AI you will receive (estimated)</span><b id="qAi">-</b></div>
  <div class="row"><span>Expected sell value (estimated)</span><b id="qSell">-</b></div>
  <div class="row"><span>Referrer (earns the referral)</span><b id="qRef" class="mono">-</b></div>
</div>
<div id="quoteWarn"></div>

<button id="mintBtn" disabled>Mint %%QTY%% shares</button>
<div id="result"></div>

<p class="small">Mint contract: <span class="mono">%%MINT%%</span><br>
AI token: <span class="mono">%%AI%%</span><br>
Referrer (W1): <span class="mono">%%REFERRER%%</span><br>
Estimates use the live pool price; the exact AI amount is confirmed
from your wallet balance after the mint. Never send BNB directly to
the contract address — BNB only goes as the mint transaction's value.</p>

<script>
"use strict";
if (window.Telegram && window.Telegram.WebApp) {
  window.Telegram.WebApp.ready();
  window.Telegram.WebApp.expand();
}

const CFG = {
  qty: %%QTY%%,
  mint: %%MINT_JSON%%,
  ai: %%AI_JSON%%,
  wbnb: %%WBNB_JSON%%,
  router: %%ROUTER_JSON%%,
  pair: %%PAIR_JSON%%,
  referrer: %%REFERRER_JSON%%,
  wcProjectId: %%WC_PROJECT_ID_JSON%%,
  enableInstantSell: %%ENABLE_INSTANT_SELL%%,
  maxCostBnb: "%%MAX_COST_BNB%%",
  rpc: %%RPC_JSON%%
};

const MINT_ABI = [
  "function costOf(uint256 shares) view returns (uint256)",
  "function totalMinted() view returns (uint256)",
  "function paidOnce(address) view returns (bool)",
  "function mint(uint256 shares, address referrer, uint256 maxCost, uint256 expectedStart, uint256 minTokens, uint256 deadline) payable"
];
const ERC20_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)"
];
const ROUTER_ABI = [
  "function getAmountsOut(uint amountIn, address[] memory path) view returns (uint[] memory amounts)",
  "function swapExactTokensForETHSupportingFeeOnTransferTokens(uint amountIn, uint amountOutMin, address[] calldata path, address to, uint deadline) returns (uint[] memory amounts)"
];
const PAIR_ABI = [
  "function token0() view returns (address)",
  "function getReserves() view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)"
];

const BSC_RPCS = [CFG.rpc, "https://bsc-dataseed.bnbchain.org",
  "https://bsc.publicnode.com", "https://1rpc.io/bnb"]
  .filter((v, i, a) => v && a.indexOf(v) === i);

const $ = (id) => document.getElementById(id);
const statusEl = $("status"), quoteEl = $("quote"), quoteWarnEl = $("quoteWarn"),
      connectBtn = $("connectBtn"), mintBtn = $("mintBtn"),
      resultEl = $("result");

let wcProvider = null, signer = null, userAddress = null;
let quote = null;  // { cost, totalMinted, paidOnce, estAi, sellOut, balanceBefore }
let bnbPrice = null;

function setStatus(html, kind) {
  statusEl.className = kind === "error" ? "error" : kind === "ok" ? "ok" : "card";
  statusEl.innerHTML = html;
}
function fail(msg) {
  setStatus("Stopped: " + msg, "error");
  mintBtn.disabled = true;
}
function shortAddr(a) { return a.slice(0, 6) + "..." + a.slice(-4); }
function fmtBnb(wei) { return ethers.formatEther(wei) + " BNB"; }
function usdOf(wei) {
  if (!bnbPrice) return "";
  return " (~$" + (parseFloat(ethers.formatEther(wei)) * bnbPrice).toFixed(2) + ")";
}
function bscLink(tx) {
  return '<a href="https://bscscan.com/tx/' + tx + '" target="_blank" rel="noopener">' + tx + '</a>';
}

// Read-only call with RPC fallback: try each public BSC RPC in turn.
async function readWithFallback(makeCall) {
  let lastErr = null;
  for (const url of BSC_RPCS) {
    try {
      const provider = new ethers.JsonRpcProvider(url);
      return await makeCall(provider);
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error("all BSC RPCs failed");
}

async function fetchBnbPrice() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT");
    const j = await r.json();
    bnbPrice = parseFloat(j.price) || null;
  } catch (e) { bnbPrice = null; }  // USD display is best-effort only
}

async function connectWallet() {
  connectBtn.disabled = true;
  try {
    if (!CFG.wcProjectId) {
      fail("Wallet connect is not configured yet — the owner still has to add a WalletConnect project ID.");
      return;
    }
    if (typeof EthereumProvider === "undefined") {
      fail("WalletConnect failed to load. Check your connection and try again.");
      return;
    }
    setStatus("Opening wallet connect&hellip; pick your wallet and approve the connection in your wallet app.");
    wcProvider = await EthereumProvider.init({
      projectId: CFG.wcProjectId,
      chains: [56],
      showQrModal: true,
      metadata: {
        name: "MintBot",
        description: "Mint Agent ID (AI) shares on BNB Smart Chain",
        url: window.location.origin,
        icons: []
      }
    });
    await wcProvider.connect();
    const provider = new ethers.BrowserProvider(wcProvider);
    signer = await provider.getSigner();
    userAddress = await signer.getAddress();

    // Must be on BNB Smart Chain (56).
    let chainId = await wcProvider.request({ method: "eth_chainId" });
    if (parseInt(chainId, 16) !== 56) {
      setStatus("Switching your wallet to BNB Smart Chain&hellip;");
      try {
        await wcProvider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x38" }] });
      } catch (e) {
        if (e && e.code === 4902) {
          await wcProvider.request({ method: "wallet_addEthereumChain", params: [{
            chainId: "0x38", chainName: "BNB Smart Chain",
            nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 },
            rpcUrls: ["https://bsc-dataseed.bnbchain.org"],
            blockExplorerUrls: ["https://bscscan.com"]
          }] });
        } else { throw e; }
      }
      chainId = await wcProvider.request({ method: "eth_chainId" });
      if (parseInt(chainId, 16) !== 56) {
        fail("Your wallet is not on BNB Smart Chain (chain 56). Switch networks and try again.");
        return;
      }
    }

    if (userAddress.toLowerCase() === CFG.referrer.toLowerCase()) {
      fail("This wallet IS the referrer wallet (W1). You cannot refer yourself — connect a different wallet.");
      return;
    }

    setStatus("Connected as " + shortAddr(userAddress) + ". Reading the live mint numbers&hellip;");
    await fetchBnbPrice();
    await loadQuote();
  } catch (e) {
    fail("Wallet connection failed: " + (e && e.message ? e.message : e));
    connectBtn.disabled = false;
  }
}

async function loadQuote() {
  try {
    const cost = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.mint, MINT_ABI, p);
      return await c.costOf(CFG.qty);
    });
    const totalMinted = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.mint, MINT_ABI, p);
      return await c.totalMinted();
    });
    const paid = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.mint, MINT_ABI, p);
      return await c.paidOnce(userAddress);
    });
    const pair = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.pair, PAIR_ABI, p);
      const t0 = await c.token0();
      const r = await c.getReserves();
      return { t0: t0.toLowerCase(), r0: r[0], r1: r[1] };
    });
    const reserveAi = pair.t0 === CFG.ai.toLowerCase() ? pair.r0 : pair.r1;
    const reserveBnb = pair.t0 === CFG.ai.toLowerCase() ? pair.r1 : pair.r0;
    if (reserveBnb === 0n) throw new Error("Pair has no BNB reserve.");

    // Estimated AI the minter receives: mint cost converted at the
    // live pool price. Labelled as an estimate; the exact amount is
    // confirmed from the wallet balance after the mint.
    const estAi = (cost * reserveAi) / reserveBnb;
    const sellOut = await readWithFallback(async (p) => {
      const r = new ethers.Contract(CFG.router, ROUTER_ABI, p);
      const amounts = await r.getAmountsOut(estAi, [CFG.ai, CFG.wbnb]);
      return amounts[1];
    });
    const balanceBefore = await readWithFallback(async (p) => {
      const t = new ethers.Contract(CFG.ai, ERC20_ABI, p);
      return await t.balanceOf(userAddress);
    });

    // Hard cap: 0.008 BNB per share.
    const maxCostWei = ethers.parseEther(CFG.maxCostBnb);
    if (cost > maxCostWei) {
      fail("Mint price is too high right now: " + fmtBnb(cost) +
        " for " + CFG.qty + " shares (cap " + CFG.maxCostBnb + " BNB). Stopped — try again later.");
      return;
    }

    quote = { cost: cost, totalMinted: totalMinted, paidOnce: paid,
              estAi: estAi, sellOut: sellOut, balanceBefore: balanceBefore };

    $("qWallet").textContent = shortAddr(userAddress);
    $("qShares").textContent = String(CFG.qty);
    $("qCost").textContent = fmtBnb(cost) + usdOf(cost);
    $("qAi").textContent = Number(ethers.formatUnits(estAi, 18)).toLocaleString("en-US") + " AI (estimated)";
    $("qSell").textContent = fmtBnb(sellOut) + usdOf(sellOut) + " (estimated)";
    $("qRef").textContent = shortAddr(CFG.referrer);
    quoteEl.hidden = false;

    let warn = "";
    if (sellOut < cost) {
      const loss = cost - sellOut;
      warn += '<div class="warn"><b>Warning: selling right now would lose money.</b><br>' +
        "Expected sell value " + fmtBnb(sellOut) + usdOf(sellOut) +
        " is below the mint cost " + fmtBnb(cost) + usdOf(cost) +
        " — a loss of about " + fmtBnb(loss) + usdOf(loss) +
        " before gas. The mint itself does not earn from the sell.</div>";
    }
    if (paid) {
      warn += '<div class="warn"><b>Note: this wallet has minted before.</b> ' +
        "The referrer is already bound from the first mint, so W1 may not earn the referral on this mint.</div>";
    }
    quoteWarnEl.innerHTML = warn;

    setStatus("Numbers are live. Check them, then press Mint if you want to go ahead.", "ok");
    mintBtn.disabled = false;
  } catch (e) {
    fail("Could not read the live mint numbers: " + (e && e.message ? e.message : e));
  }
}

async function doMint() {
  mintBtn.disabled = true;
  try {
    if (!quote || !signer) throw new Error("No live quote — connect and load the numbers first.");
    setStatus("Re-reading the live price right before sending&hellip;");
    const cost = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.mint, MINT_ABI, p);
      return await c.costOf(CFG.qty);
    });
    const totalMinted = await readWithFallback(async (p) => {
      const c = new ethers.Contract(CFG.mint, MINT_ABI, p);
      return await c.totalMinted();
    });
    const maxCostWei = ethers.parseEther(CFG.maxCostBnb);
    if (cost > maxCostWei) {
      fail("Mint price rose above the cap (" + fmtBnb(cost) + " > " + CFG.maxCostBnb + " BNB). Stopped.");
      return;
    }
    const expectedStart = totalMinted + 1n;
    const deadline = Math.floor(Date.now() / 1000) + 600;

    // Simulate first: if the mint would revert (e.g. no .ai Agent ID),
    // stop here and show the error instead of sending a failing tx.
    setStatus("Simulating the mint&hellip;");
    const mintWrite = new ethers.Contract(CFG.mint, MINT_ABI, signer);
    try {
      await mintWrite.mint.staticCall(CFG.qty, CFG.referrer, cost, expectedStart, 0, deadline, { value: cost });
    } catch (e) {
      fail("The mint would fail on-chain: " + (e && e.message ? e.message : e) +
        " Nothing was sent. (A common cause: this wallet holds no .ai Agent ID.)");
      return;
    }

    setStatus("Sending the mint — approve it in your wallet app&hellip;");
    const tx = await mintWrite.mint(CFG.qty, CFG.referrer, cost, expectedStart, 0, deadline, { value: cost });
    resultEl.innerHTML = '<div class="ok">Mint sent: ' + bscLink(tx.hash) + '<br>Waiting for confirmation&hellip;</div>';
    const receipt = await tx.wait();
    if (receipt.status !== 1) {
      fail("Mint transaction failed on-chain: " + bscLink(tx.hash));
      return;
    }
    resultEl.innerHTML = '<div class="ok">Mint confirmed! ' + bscLink(tx.hash) + '</div>';
    setStatus("Mint confirmed.", "ok");

    if (!CFG.enableInstantSell) {
      resultEl.innerHTML += '<div class="card">Instant sell is OFF — your AI stays in your wallet. ' +
        "You can sell it yourself later if you want.</div>";
      return;
    }
    await doSell();
  } catch (e) {
    fail("Mint failed: " + (e && e.message ? e.message : e));
  }
}

async function doSell() {
  try {
    setStatus("Instant sell is ON — checking the AI you actually received&hellip;");
    const balanceAfter = await readWithFallback(async (p) => {
      const t = new ethers.Contract(CFG.ai, ERC20_ABI, p);
      return await t.balanceOf(userAddress);
    });
    const minted = balanceAfter - quote.balanceBefore;
    if (minted <= 0n) {
      fail("No new AI arrived in your wallet after the mint, so there is nothing to sell.");
      return;
    }
    const expectedOut = await readWithFallback(async (p) => {
      const r = new ethers.Contract(CFG.router, ROUTER_ABI, p);
      const amounts = await r.getAmountsOut(minted, [CFG.ai, CFG.wbnb]);
      return amounts[1];
    });
    const amountOutMin = (expectedOut * 95n) / 100n;  // 5% slippage guard
    const ai = new ethers.Contract(CFG.ai, ERC20_ABI, signer);
    const router = new ethers.Contract(CFG.router, ROUTER_ABI, signer);
    setStatus("Approving the router to sell exactly " +
      Number(ethers.formatUnits(minted, 18)).toLocaleString("en-US") + " AI&hellip;");
    const approveTx = await ai.approve(CFG.router, minted);
    await approveTx.wait();
    setStatus("Selling — approve the swap in your wallet app&hellip;");
    const deadline = Math.floor(Date.now() / 1000) + 600;
    const swapTx = await router.swapExactTokensForETHSupportingFeeOnTransferTokens(
      minted, amountOutMin, [CFG.ai, CFG.wbnb], userAddress, deadline);
    resultEl.innerHTML += '<div class="ok">Sell sent: ' + bscLink(swapTx.hash) + '<br>Waiting for confirmation&hellip;</div>';
    const receipt = await swapTx.wait();
    if (receipt.status !== 1) {
      fail("Sell transaction failed on-chain: " + bscLink(swapTx.hash));
      return;
    }
    resultEl.innerHTML += '<div class="ok">Sell confirmed! ' + bscLink(swapTx.hash) +
      '<br>Minimum expected back: ' + fmtBnb(amountOutMin) + ' BNB.</div>';
    setStatus("Done — minted and sold.", "ok");
  } catch (e) {
    fail("Sell failed: " + (e && e.message ? e.message : e) +
      " Your AI is still in your wallet.");
  }
}

connectBtn.addEventListener("click", connectWallet);
mintBtn.addEventListener("click", doMint);

if (!CFG.wcProjectId) {
  setStatus("Wallet connect is not configured yet — the owner still has to add a WalletConnect project ID. You cannot connect a wallet until then.", "error");
} else if (typeof ethers === "undefined" || typeof EthereumProvider === "undefined") {
  setStatus("Wallet libraries failed to load. Check your connection and reopen this page.", "error");
} else {
  setStatus("Press Connect Wallet to begin. Your wallet stays yours — the page only asks it to sign the mint.");
  connectBtn.disabled = false;
}
</script>
</body>
</html>
"""


def render_mint_page(qty, cfg):
    """Return the mint page HTML for qty (5 or 10) and a config dict.

    cfg keys: mint_contract, ai_token, wbnb_token, pancake_router,
    ai_pair, referrer, wc_project_id, enable_instant_sell (bool),
    max_cost_bnb (str, e.g. "0.08"), bsc_rpc.
    """
    wc_warning = ""
    if not cfg.get("wc_project_id"):
        wc_warning = (
            '<div class="error"><b>Wallet connect is not configured yet.</b><br>'
            "The owner still has to add a WalletConnect project ID before "
            "any wallet can connect here.</div>"
        )
    html = _PAGE_HTML
    replacements = {
        "%%QTY%%": str(qty),
        "%%MINT%%": cfg["mint_contract"],
        "%%AI%%": cfg["ai_token"],
        "%%REFERRER%%": cfg["referrer"],
        "%%MINT_JSON%%": json.dumps(cfg["mint_contract"]),
        "%%AI_JSON%%": json.dumps(cfg["ai_token"]),
        "%%WBNB_JSON%%": json.dumps(cfg["wbnb_token"]),
        "%%ROUTER_JSON%%": json.dumps(cfg["pancake_router"]),
        "%%PAIR_JSON%%": json.dumps(cfg["ai_pair"]),
        "%%REFERRER_JSON%%": json.dumps(cfg["referrer"]),
        "%%WC_PROJECT_ID_JSON%%": json.dumps(cfg.get("wc_project_id") or ""),
        "%%ENABLE_INSTANT_SELL%%": "true" if cfg.get("enable_instant_sell") else "false",
        "%%MAX_COST_BNB%%": str(cfg["max_cost_bnb"]),
        "%%RPC_JSON%%": json.dumps(cfg.get("bsc_rpc") or ""),
        "%%WC_WARNING%%": wc_warning,
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html
