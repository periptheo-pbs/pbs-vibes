#!/usr/bin/env python3
"""
HL Execution Module — Place and manage trades on Hyperliquid.

Usage:
  python3 hl_execute.py open BTC long 0.001 5      # open long 0.001 BTC at 5x
  python3 hl_execute.py close BTC                   # close all BTC
  python3 hl_execute.py close BTC 0.001             # close partial
  python3 hl_execute.py leverage BTC 10             # set leverage to 10x
  python3 hl_execute.py status                      # show positions + equity
"""

import json
import os
import sys
import time
from pathlib import Path
from decimal import Decimal

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ── Config ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

USE_TESTNET = os.environ.get("USE_TESTNET", "true").lower() in ("true", "1", "yes")
BASE_URL = constants.TESTNET_API_URL if USE_TESTNET else constants.MAINNET_API_URL

MAIN_WALLET = os.environ.get("HL_MAIN_WALLET", "")
API_SECRET = os.environ.get("HL_SECRET_KEY", "")
API_WALLET = os.environ.get("HL_WALLET_API", "")

def _fmt_px(px: float) -> str:
    """Format a price with adaptive decimals for sub-$1 assets."""
    if px < 0.01:
        return f"${px:,.6f}"
    if px < 1:
        return f"${px:,.4f}"
    return f"${px:,.0f}"


# ── Clients ────────────────────────────────────────────────────────────
def get_info():
    return Info(BASE_URL, skip_ws=True)

def get_exchange():
    """Create Exchange client with API wallet."""
    if not API_SECRET:
        raise ValueError("HL_SECRET_KEY not set in .env")
    wallet = Account.from_key(API_SECRET)
    return Exchange(wallet, base_url=BASE_URL, account_address=MAIN_WALLET)

# ── Portfolio ──────────────────────────────────────────────────────────
def get_portfolio():
    """Get full portfolio state from HL."""
    info = get_info()
    result = {"wallet": MAIN_WALLET, "testnet": USE_TESTNET}

    # Perp state
    perp = info.user_state(MAIN_WALLET)
    if perp:
        ms = perp.get("marginSummary", {})
        result["perp_equity"] = float(ms.get("accountValue", 0))
        result["total_margin_used"] = float(ms.get("totalMarginUsed", 0))
        result["total_ntl_pos"] = float(ms.get("totalNtlPos", 0))
        result["withdrawable"] = float(perp.get("withdrawable", 0))

        positions = []
        for ap in perp.get("assetPositions", []):
            p = ap.get("position", {})
            szi = float(p.get("szi", 0))
            if szi == 0:
                continue
            positions.append({
                "coin": p.get("coin", "?"),
                "side": "long" if szi > 0 else "short",
                "size": abs(szi),
                "entry": float(p.get("entryPx", 0)),
                "leverage": float(p.get("leverage", {}).get("value", 0)) if isinstance(p.get("leverage"), dict) else float(p.get("leverage", 0)),
                "upnl": float(p.get("unrealizedPnl", 0)),
                "liq_px": float(p.get("liquidationPx", 0) or 0),
                "margin_used": float(p.get("marginUsed", 0)),
            })
        result["positions"] = positions
    else:
        result["perp_equity"] = 0.0
        result["positions"] = []

    # Spot state
    spot = info.spot_user_state(MAIN_WALLET)
    spot_total = 0.0
    if spot:
        for bal in spot.get("balances", []):
            total = float(bal.get("total", 0))
            if total > 0 and "USDC" in bal.get("coin", ""):
                spot_total += total
    result["spot_balance"] = spot_total
    result["total_equity"] = result["perp_equity"] + spot_total

    return result


# ── Trading ────────────────────────────────────────────────────────────
def set_leverage(coin: str, leverage: int, cross: bool = True):
    """Set leverage for a coin."""
    ex = get_exchange()
    result = ex.update_leverage(leverage, coin, cross)
    return result


def open_position(coin: str, is_buy: bool, size: float, leverage: int = None, 
                  limit_px: float = None, slippage: float = 0.005):
    """
    Open a position.
    - coin: 'BTC', 'ETH', 'SOL'
    - is_buy: True=long, False=short
    - size: asset amount (e.g., 0.001 BTC)
    - leverage: set before opening (optional)
    - limit_px: limit price (None = market)
    - slippage: for market orders (default 0.5%)
    """
    ex = get_exchange()

    # Set leverage if specified
    if leverage is not None:
        ex.update_leverage(leverage, coin, True)

    if limit_px:
        # Limit order
        order_type = {"limit": {"tif": "Gtc"}}
        result = ex.order(coin, is_buy, size, limit_px, order_type)
    else:
        # Market order
        result = ex.market_open(coin, is_buy, size, px=limit_px, slippage=slippage)

    return result


def close_position(coin: str, size: float = None, limit_px: float = None, slippage: float = 0.005):
    """
    Close a position (fully or partially).
    - coin: 'BTC', 'ETH', 'SOL'
    - size: amount to close (None = close all)
    - limit_px: limit price (None = market)
    """
    ex = get_exchange()

    if limit_px:
        # Need to determine side and place reduce-only limit
        info = get_info()
        perp = info.user_state(MAIN_WALLET)
        for ap in perp.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") == coin:
                szi = float(p.get("szi", 0))
                if szi == 0:
                    return {"status": "no_position"}
                is_buy = szi < 0  # close long = sell, close short = buy
                close_size = abs(szi) if size is None else size
                order_type = {"limit": {"tif": "Gtc"}}
                return ex.order(coin, is_buy, close_size, limit_px, 
                               order_type, reduce_only=True)
        return {"status": "no_position"}
    else:
        return ex.market_close(coin, sz=size, slippage=slippage)


# ── CLI ────────────────────────────────────────────────────────────────
def print_status():
    """Print current portfolio status."""
    p = get_portfolio()
    net = "TESTNET" if p["testnet"] else "MAINNET"
    print(f"\n{'='*50}")
    print(f"  HL Portfolio ({net})")
    print(f"{'='*50}")
    avail = p['spot_balance'] - p.get('total_margin_used', 0)
    print(f"  Total Equity:   ${p['total_equity']:,.2f}")
    print(f"  Perp Equity:    ${p['perp_equity']:,.2f}")
    print(f"  Spot Balance:   ${p['spot_balance']:,.2f}")
    print(f"  Margin Used:    ${p.get('total_margin_used', 0):,.2f}")
    print(f"  AVAILABLE:      ${avail:,.2f}  (spot - margin used, for new positions)")
    print(f"  Withdrawable:   ${p.get('withdrawable', 0):,.2f}  (maintenance buffer only — NOT available capital)")

    if p["positions"]:
        print(f"\n  Positions ({len(p['positions'])}):")
        for pos in p["positions"]:
            side = pos["side"].upper()
            pnl = pos["upnl"]
            pnl_s = "+" if pnl >= 0 else ""
            liq = pos['liq_px']
            liq_s = f"  liq {_fmt_px(liq)}" if liq > 0 else ""
            print(f"    {side} {pos['size']:.5f} {pos['coin']} @ {_fmt_px(pos['entry'])} "
                  f"({pos['leverage']:.0f}x)  PnL {pnl_s}${pnl:,.2f}{liq_s}")
    else:
        print(f"\n  Positions: NONE")

    # Open orders
    info = get_info()
    orders = info.open_orders(MAIN_WALLET)
    if orders:
        print(f"\n  Open Orders ({len(orders)}):")
        for o in orders:
            print(f"    {o.get('side', '?')} {o.get('sz', '?')} {o.get('coin', '?')} @ ${o.get('limitPx', '?')}")
    else:
        print(f"\n  Open Orders: none")
    print()


def main():
    if len(sys.argv) < 2:
        print_status()
        return

    cmd = sys.argv[1].lower()

    if cmd == "status":
        print_status()

    elif cmd == "open":
        if len(sys.argv) < 5:
            print("Usage: hl_execute.py open COIN SIDE SIZE [LEVERAGE]")
            print("  COIN: BTC, ETH, SOL")
            print("  SIDE: long, short")
            print("  SIZE: asset amount (e.g., 0.001)")
            print("  LEVERAGE: optional (e.g., 5)")
            return
        coin = sys.argv[2].upper()
        is_buy = sys.argv[3].lower() == "long"
        size = float(sys.argv[4])
        leverage = int(sys.argv[5]) if len(sys.argv) > 5 else None

        print(f"Opening {sys.argv[3]} {size} {coin}" + (f" at {leverage}x" if leverage else " (market)") + "...")
        result = open_position(coin, is_buy, size, leverage=leverage)
        print(f"Result: {json.dumps(result, indent=2) if isinstance(result, dict) else result}")
        print_status()

    elif cmd == "close":
        if len(sys.argv) < 3:
            print("Usage: hl_execute.py close COIN [SIZE]")
            return
        coin = sys.argv[2].upper()
        size = float(sys.argv[3]) if len(sys.argv) > 3 else None

        print(f"Closing {coin}" + (f" {size}" if size else " (all)") + "...")
        result = close_position(coin, size=size)
        print(f"Result: {json.dumps(result, indent=2) if isinstance(result, dict) else result}")
        print_status()

    elif cmd == "leverage":
        if len(sys.argv) < 4:
            print("Usage: hl_execute.py leverage COIN LEVERAGE")
            return
        coin = sys.argv[2].upper()
        lev = int(sys.argv[3])
        print(f"Setting {coin} leverage to {lev}x...")
        result = set_leverage(coin, lev)
        print(f"Result: {result}")

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: status, open, close, leverage")


if __name__ == "__main__":
    main()
