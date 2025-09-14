"""
Flask backend for Payme Crypto frontend.

Provides:
- /api/coins/markets?ids=...
- /api/coins/search?q=...
- /api/balance  (POST)
- /api/balance/multi (POST)  <-- NEW (non-destructive): accepts {"addresses": {"ethereum":"0x..", "solana":".."}, "coin_ids":[...], "tokens":[...]}.

Install:
  pip install -r requirements.txt
Run:
  python app.py

Notes:
- Configure RPC endpoints in RPC_URLS dictionary (replace public RPCs with Alchemy/Infura for production).
- Optional Solana support: `pip install solana` (app will use it if available).
- Optional YAML support for chains config: `pip install pyyaml`.
"""
import os
import time
import json
import logging
from functools import lru_cache
from typing import Dict, Any, List, Optional

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from web3 import Web3, HTTPProvider
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

# Optional solana support (will be used only if installed)
try:
    from solana.rpc.api import Client as SolanaClient
    from solana.publickey import PublicKey
    SOLANA_AVAILABLE = True
except Exception:
    SOLANA_AVAILABLE = False

# Optional YAML support
try:
    import yaml
    YAML_AVAILABLE = True
except Exception:
    YAML_AVAILABLE = False

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log incoming requests (you asked where to put it — here is before_request)
@app.before_request
def log_request():
    logger.info(f"Incoming request: {request.method} {request.path}")

# -------------------------
# Config
# -------------------------
COINGECKO_API = "https://api.coingecko.com/api/v3"

# Default RPC endpoints: will be merged with chains file (if provided)
# Keep defaults non-destructive (same as your earlier code)
RPC_URLS: Dict[str, str] = {
    "ethereum": os.environ.get("RPC_ETH", "https://cloudflare-eth.com"),
    "bsc": os.environ.get("RPC_BSC", "https://bsc-dataseed.binance.org/"),
    "polygon": os.environ.get("RPC_POLYGON", "https://rpc.ankr.com/polygon"),
    "avax": os.environ.get("RPC_AVAX", "https://rpc.ankr.com/avalanche"),
    "arbitrum": os.environ.get("RPC_ARBI", "https://rpc.ankr.com/arbitrum"),
    "optimism": os.environ.get("RPC_OPT", "https://rpc.ankr.com/optimism"),
    "fantom": os.environ.get("RPC_FANTOM", "https://rpc.ankr.com/fantom"),
    "cronos": os.environ.get("RPC_CRONOS", "https://evm.cronos.org"),
    # Solana uses a different SDK (optional)
    "solana": os.environ.get("RPC_SOLANA", "https://api.mainnet-beta.solana.com"),
}

# minimal ERC20 ABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

# map coinGecko platform key -> our RPC key (kept intact)
PLATFORM_TO_CHAIN_KEY = {
    "ethereum": "ethereum",
    "binance-smart-chain": "bsc",
    "polygon-pos": "polygon",
    "avalanche": "avax",
    "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism",
    "fantom": "fantom",
    "solana": "solana",
}

# native coin coinGecko id -> chain key (kept intact)
NATIVE_COIN_TO_CHAIN = {
    "ethereum": "ethereum",
    "binancecoin": "bsc",
    "polygon": "polygon",
    "avalanche-2": "avax",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "fantom": "fantom",
    "solana": "solana",
}

# small in-memory caches
_MARKETS_CACHE: Dict[str, Any] = {}       # key -> (timestamp, data)
_COIN_DETAIL_CACHE: Dict[str, Any] = {}   # coinId -> (timestamp, data)
_CACHE_TTL = 12  # seconds for markets, short because frontend refreshes fast

# create Web3 providers lazily and reuse
_WEB3_PROVIDERS: Dict[str, Web3] = {}

# config file state for chains
_CHAIN_CONFIG_PATH = os.environ.get("CHAIN_CONFIG_PATH", None)
if not _CHAIN_CONFIG_PATH:
    if os.path.exists("chains.yaml"):
        _CHAIN_CONFIG_PATH = "chains.yaml"
    elif os.path.exists("chains.json"):
        _CHAIN_CONFIG_PATH = "chains.json"
    else:
        _CHAIN_CONFIG_PATH = None
_CHAIN_CONFIG_MTIME: Optional[float] = None

def load_chains_from_env_json() -> Dict[str, str]:
    cfg = os.environ.get("CHAIN_CONFIG_JSON")
    if not cfg:
        return {}
    try:
        parsed = json.loads(cfg)
        result = {}
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if isinstance(v, str):
                    result[k] = v
                elif isinstance(v, dict) and v.get("rpc"):
                    result[k] = v.get("rpc")
        return result
    except Exception:
        app.logger.warning("Failed to parse CHAIN_CONFIG_JSON env var")
        return {}

def load_chains_file(path: str) -> Dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
            if YAML_AVAILABLE:
                try:
                    parsed = yaml.safe_load(raw)
                except Exception:
                    parsed = None
            else:
                parsed = None
            if parsed is None:
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
            if not parsed or not isinstance(parsed, dict):
                return {}
            out = {}
            for k, v in parsed.items():
                if isinstance(v, str):
                    out[k] = v
                elif isinstance(v, dict):
                    if "rpc" in v and isinstance(v["rpc"], str):
                        out[k] = v["rpc"]
            return out
    except Exception as e:
        app.logger.warning("Failed to load chains file %s: %s", path, e)
        return {}

def reload_rpc_config_if_changed():
    global RPC_URLS, _CHAIN_CONFIG_MTIME
    envcfg = load_chains_from_env_json()
    if envcfg:
        for k, v in envcfg.items():
            RPC_URLS[k] = v
    if not _CHAIN_CONFIG_PATH:
        return
    try:
        mtime = os.path.getmtime(_CHAIN_CONFIG_PATH)
    except Exception:
        return
    if _CHAIN_CONFIG_MTIME is None or mtime != _CHAIN_CONFIG_MTIME:
        new_map = load_chains_file(_CHAIN_CONFIG_PATH)
        if new_map:
            for k, v in new_map.items():
                RPC_URLS[k] = v
            app.logger.info("Loaded %d chains from %s", len(new_map), _CHAIN_CONFIG_PATH)
        else:
            app.logger.info("No valid chain entries found in %s", _CHAIN_CONFIG_PATH)
        _CHAIN_CONFIG_MTIME = mtime

def get_web3(chain_key: str) -> Optional[Web3]:
    try:
        reload_rpc_config_if_changed()
    except Exception:
        app.logger.debug("reload_rpc_config_if_changed failed (ignored)")

    if chain_key not in RPC_URLS:
        return None
    if chain_key in _WEB3_PROVIDERS:
        return _WEB3_PROVIDERS[chain_key]
    try:
        provider = HTTPProvider(RPC_URLS[chain_key], request_kwargs={"timeout": 20})
        w3 = Web3(provider)
        _WEB3_PROVIDERS[chain_key] = w3
        return w3
    except Exception as e:
        app.logger.warning("Failed to create Web3 for %s: %s", chain_key, e)
        return None

# -------------------------
# CoinGecko helpers
# -------------------------
def cached_markets(ids: List[str]) -> List[dict]:
    if not ids:
        return []
    key = ",".join(sorted(ids))
    now = time.time()
    entry = _MARKETS_CACHE.get(key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["data"]
    try:
        params = {
            "vs_currency": "usd",
            "ids": key,
            "order": "market_cap_desc",
            "per_page": len(ids),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        }
        r = requests.get(f"{COINGECKO_API}/coins/markets", params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        _MARKETS_CACHE[key] = {"ts": now, "data": data}
        return data
    except Exception as e:
        app.logger.warning("CoinGecko markets fetch failed for %s: %s", key, e)
        return []

def cached_coin_detail(coin_id: str) -> Optional[dict]:
    now = time.time()
    entry = _COIN_DETAIL_CACHE.get(coin_id)
    if entry and now - entry["ts"] < 60:
        return entry["data"]
    try:
        r = requests.get(
            f"{COINGECKO_API}/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "market_data": "false",
                    "community_data": "false", "developer_data": "false", "sparkline": "false"},
            timeout=12
        )
        r.raise_for_status()
        data = r.json()
        _COIN_DETAIL_CACHE[coin_id] = {"ts": now, "data": data}
        return data
    except Exception as e:
        app.logger.warning("CoinGecko coin detail failed for %s: %s", coin_id, e)
        return None

def find_contract_for_coin_id(coin_id: str) -> Optional[Dict[str, str]]:
    detail = cached_coin_detail(coin_id)
    if not detail:
        return None
    platforms = detail.get("platforms") or {}
    prefer = ["ethereum", "binance-smart-chain", "polygon-pos", "avalanche", "arbitrum-one", "optimistic-ethereum"]
    for p in prefer:
        val = platforms.get(p)
        if val:
            return {"contract": val, "platform": p}
    for p, val in platforms.items():
        if val:
            return {"contract": val, "platform": p}
    return None

# -------------------------
# Balance helpers
# -------------------------
def get_native_evm_balance(chain_key: str, address: str) -> Optional[float]:
    w3 = get_web3(chain_key)
    if w3 is None:
        return None
    try:
        checksum = Web3.toChecksumAddress(address)
        bal_wei = w3.eth.get_balance(checksum)
        return float(w3.fromWei(bal_wei, "ether"))
    except Exception as e:
        app.logger.warning("native balance error for %s @ %s: %s", chain_key, address, e)
        return None

def get_erc20_balance(chain_key: str, contract_address: str, address: str) -> Optional[float]:
    w3 = get_web3(chain_key)
    if w3 is None:
        return None
    try:
        checksum_token = Web3.toChecksumAddress(contract_address)
        checksum_addr = Web3.toChecksumAddress(address)
        token = w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        try:
            decimals = token.functions.decimals().call()
        except Exception:
            decimals = 18
        raw = token.functions.balanceOf(checksum_addr).call()
        return float(raw) / (10 ** decimals)
    except (BadFunctionCallOutput, ContractLogicError) as e:
        app.logger.warning("erc20 contract call failed: %s", e)
        return None
    except Exception as e:
        app.logger.warning("erc20 balance error for %s on %s: %s", contract_address, chain_key, e)
        return None

def get_solana_balance(address: str) -> Optional[float]:
    if not SOLANA_AVAILABLE:
        return None
    try:
        client = SolanaClient(RPC_URLS.get("solana") or "https://api.mainnet-beta.solana.com")
        pk = PublicKey(address)
        resp = client.get_balance(pk)
        if resp.get("result") and resp["result"].get("value") is not None:
            lamports = resp["result"]["value"]
            return lamports / 1e9  # SOL has 1e9 lamports
    except Exception as e:
        app.logger.warning("Solana balance error for %s: %s", address, e)
    return None

# -------------------------
# Core: compute balance helper (refactored so both endpoints can reuse)
# -------------------------
def compute_balance_for_chain(chain: str, address: str, coin_ids: List[str], tokens: List[Dict[str, Any]]):
    """
    Compute result similar to your /api/balance endpoint, but as a function.
    """
    result = {
        "chain": chain,
        "address": address,
        "native": None,
        "tokens": [],
        "errors": []
    }

    try:
        if chain in RPC_URLS and chain != "solana":
            native_bal = get_native_evm_balance(chain, address)
            native_coin_id = None
            for cid, ch in NATIVE_COIN_TO_CHAIN.items():
                if ch == chain:
                    native_coin_id = cid
                    break
            price = None
            price_change = None
            if native_coin_id:
                markets = cached_markets([native_coin_id])
                if markets:
                    m = markets[0]
                    price = float(m.get("current_price", 0) or 0)
                    price_change = m.get("price_change_percentage_24h")
            result["native"] = {
                "symbol": native_coin_id.upper() if native_coin_id else chain.upper(),
                "balance": native_bal if native_bal is not None else 0.0,
                "usd_price": price,
                "usd_value": (native_bal or 0) * (price or 0),
                "price_change_24h": price_change
            }
        elif chain == "solana":
            sol_bal = get_solana_balance(address)
            price = None
            m = cached_markets(["solana"])
            if m:
                price = float(m[0].get("current_price", 0) or 0)
            result["native"] = {
                "symbol": "SOL",
                "balance": sol_bal if sol_bal is not None else 0.0,
                "usd_price": price,
                "usd_value": (sol_bal or 0) * (price or 0),
                "price_change_24h": m[0].get("price_change_percentage_24h") if m else None
            }
        else:
            result["native"] = {"symbol": chain.upper(), "balance": 0.0, "usd_price": None, "usd_value": 0.0}
    except Exception as e:
        app.logger.exception("native balance fetch failed")
        result["errors"].append(f"native_balance_error: {str(e)}")

    # coin_ids price lookup
    coin_ids = list(dict.fromkeys([str(cid).strip() for cid in (coin_ids or []) if cid]))
    markets = {}
    if coin_ids:
        market_data = cached_markets(coin_ids)
        for m in market_data:
            if m.get("id"):
                markets[m["id"]] = m

    for coin_id in coin_ids:
        if coin_id in NATIVE_COIN_TO_CHAIN and NATIVE_COIN_TO_CHAIN[coin_id] == chain:
            continue
        found = find_contract_for_coin_id(coin_id)
        if found and found.get("contract"):
            platform = found.get("platform")
            chain_key = PLATFORM_TO_CHAIN_KEY.get(platform)
            contract_addr = found.get("contract")
            balance = None
            if chain_key and chain_key in RPC_URLS:
                balance = get_erc20_balance(chain_key, contract_addr, address)
            m = markets.get(coin_id) or {}
            usd_price = float(m.get("current_price", 0) or 0)
            price_chg = m.get("price_change_percentage_24h")
            result["tokens"].append({
                "coin_id": coin_id,
                "symbol": (m.get("symbol") or "").upper() or coin_id.upper(),
                "name": m.get("name") or coin_id,
                "contract": contract_addr,
                "platform": platform,
                "chain": chain_key,
                "balance": balance if balance is not None else 0.0,
                "usd_price": usd_price,
                "price_change_24h": price_chg,
                "usd_value": (balance or 0.0) * (usd_price or 0.0),
                "logo": m.get("image")
            })
        else:
            m = markets.get(coin_id)
            price = float(m.get("current_price", 0) or 0) if m else None
            result["tokens"].append({
                "coin_id": coin_id,
                "symbol": (m.get("symbol") or coin_id).upper() if m else coin_id.upper(),
                "name": m.get("name") or coin_id,
                "contract": None,
                "platform": None,
                "chain": None,
                "balance": 0.0,
                "usd_price": price,
                "price_change_24h": m.get("price_change_percentage_24h") if m else None,
                "usd_value": 0.0,
                "logo": m.get("image") if m else None
            })

    # explicit tokens list
    for t in tokens or []:
        try:
            tchain = t.get("chain") or chain
            tcontract = t.get("contract")
            if not tcontract:
                continue
            balance = get_erc20_balance(tchain, tcontract, address)
            usd_price = None
            price_chg = None
            logo = None
            coin_name = None
            coin_symbol = None
            try:
                platform_for_cg = None
                for pf, ck in PLATFORM_TO_CHAIN_KEY.items():
                    if ck == tchain:
                        platform_for_cg = pf
                        break
                if platform_for_cg:
                    r = requests.get(f"{COINGECKO_API}/coins/{platform_for_cg}/contract/{tcontract}", timeout=10)
                    if r.ok:
                        info = r.json()
                        market = info.get("market_data") or {}
                        usd_price = (market.get("current_price") or {}).get("usd")
                        price_chg = market.get("price_change_percentage_24h")
                        coin_name = info.get("name")
                        coin_symbol = info.get("symbol")
                        logo = (info.get("image") or {}).get("small")
            except Exception:
                pass

            result["tokens"].append({
                "coin_id": None,
                "symbol": (coin_symbol or t.get("symbol") or "").upper() or tcontract[:6],
                "name": coin_name or t.get("name") or tcontract,
                "contract": tcontract,
                "platform": None,
                "chain": tchain,
                "balance": balance if balance is not None else 0.0,
                "usd_price": float(usd_price) if usd_price else None,
                "price_change_24h": price_chg,
                "usd_value": (balance or 0.0) * (float(usd_price) if usd_price else 0.0),
                "logo": logo
            })
        except Exception as e:
            app.logger.exception("token entry failure")
            result["errors"].append(f"token_error:{str(e)}")

    # totals
    total_usd = 0.0
    if result["native"] and result["native"].get("usd_value"):
        total_usd += float(result["native"]["usd_value"] or 0.0)
    for tk in result["tokens"]:
        try:
            total_usd += float(tk.get("usd_value") or 0.0)
        except Exception:
            pass
    result["total_usd"] = total_usd

    return result

# -------------------------
# API endpoints
# -------------------------
@app.route("/api/coins/markets", methods=["GET"])
def api_coins_markets():
    ids = request.args.get("ids", "")
    if not ids:
        return jsonify({"error": "ids query param required"}), 400
    ids_list = [i.strip() for i in ids.split(",") if i.strip()]
    data = cached_markets(ids_list)
    return jsonify(data)


@app.route("/api/coins/search", methods=["GET"])
def api_coins_search():
    q = request.args.get("q") or request.args.get("query")
    if not q:
        return jsonify({"error": "q parameter required"}), 400
    try:
        r = requests.get(f"{COINGECKO_API}/search", params={"query": q}, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        app.logger.exception("CoinGecko search failed")
        return jsonify({"error": "search failed", "detail": str(e)}), 500


@app.route("/api/balance", methods=["POST"])
def api_balance():
    payload = request.get_json(force=True, silent=True) or {}
    chain = (payload.get("chain") or "").strip().lower()
    address = (payload.get("address") or "").strip()
    coin_ids = payload.get("coin_ids") or []
    tokens = payload