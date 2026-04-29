#!/usr/bin/env python3 -u
"""
Vibes Trader Unified Runner — Bulletproof wrapper.
Runs kz_calculator.py, reads state, formats clean Telegram summary.
NO AI analysis — just real data + state + execution.
"""
from __future__ import annotations
import datetime
import json
import os
import subprocess
import sys
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PROJECT_ROOT, "live", "hermes_vibes_state.json")
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")

def load_env():
    """Load .env file into os.environ."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()

def run_kz_calculator(asset: str = "BTC") -> str:
    """Run kz_calculator.py and return FULL output."""
    script = os.path.join(PROJECT_ROOT, "scripts", "kz_calculator.py")
    result = subprocess.run(
        [sys.executable, script, "--asset", asset],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout + (result.stderr if result.stderr else "")

def read_state() -> dict:
    """Read vibes state file."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"in_position": False, "side": "none", "size_btc": 0.0}

def fetch_hl_price(coin: str = "BTC") -> float:
    """Get current price from HL."""
    try:
        resp = requests.post("https://api.hyperliquid.xyz/info",
                           json={"type": "allMids"}, timeout=5)
        mids = resp.json()
        if isinstance(mids, dict):
            price = mids.get(coin)
            if price:
                return float(price)
    except Exception:
        pass
    return 0.0

def execute_trade(action: str, size: float, leverage: int, bps: int, price: float) -> str:
    """Execute trade via vibes_execute.py."""
    script = os.path.join(PROJECT_ROOT, "scripts", "vibes_execute.py")
    secret = os.environ.get("HL_SECRET_KEY", "")
    wallet = os.environ.get("HL_MAIN_WALLET", "")
    
    cmd = [
        sys.executable, script,
        "--action", action,
        "--size", str(size),
        "--leverage", str(leverage),
        "--bps", str(bps),
        "--price", str(price),
        "--secret-key", secret,
        "--wallet", wallet
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return result.stdout + (result.stderr if result.stderr else "")

def format_telegram_summary(kz_output: str, state: dict, price: float, trade_result: str = "") -> str:
    """Format clean Telegram message with real data only."""
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # Extract key info from kz_output
    lines = kz_output.split('\n')
    planet_line = next((l for l in lines if 'Planet:' in l), 'Planet: ?')
    price_line = next((l for l in lines if 'Current Price:' in l), f'Current Price: ${price:,.0f}' if price else 'Current Price: —')
    
    # Build message
    msg = f"⚡ **Vibes Trader Unified** — {now:%Y-%m-%d %H:%M UTC}\n\n"
    
    # State
    if state.get("in_position"):
        msg += f"📊 **Position**: {state['side'].upper()} {state['size_btc']:.4f} BTC @ ${state.get('entry_price', 0):,.2f}\n"
    else:
        msg += f"📊 **Position**: NONE\n"
    
    # Real KZ data (first 20 lines)
    msg += f"\n{kz_output[:1500]}\n"
    
    # Trade result
    if trade_result:
        msg += f"\n⚙️ **Execution**:\n{trade_result}\n"
    
    msg += f"\n_Updated: {now:%Y-%m-%d %H:%M:%S UTC}_"
    
    return msg

def main():
    load_env()
    
    # Step 1: Run real kz_calculator
    print("=== Running kz_calculator.py ===", flush=True)
    kz_output = run_kz_calculator("BTC")
    print(kz_output)
    
    # Step 2: Read state
    state = read_state()
    print(f"\n=== State: {json.dumps(state)} ===", flush=True)
    
    # Step 3: Get current price
    price = fetch_hl_price("BTC")
    
    # Step 4: Simple decision logic (rule-based, no AI)
    action = "hold"
    size = 0.015
    leverage = 2
    bps = 4
    
    if not state.get("in_position"):
        # Check if we should enter (simple: enter long during bullish planets)
        if "Jupiter" in kz_output or "Venus" in kz_output or "Sun" in kz_output:
            action = "enter_long"
        elif "Saturn" in kz_output or "Moon" in kz_output:
            action = "enter_short"
    
    # Step 5: Execute if not hold
    trade_result = ""
    if action != "hold" and price > 0:
        print(f"\n=== Executing: {action} ===", flush=True)
        trade_result = execute_trade(action, size, leverage, bps, price)
        print(trade_result)
    
    # Step 6: Format and print Telegram summary
    summary = format_telegram_summary(kz_output, state, price, trade_result)
    print("\n" + "="*60)
    print("TELEGRAM SUMMARY:")
    print("="*60)
    print(summary)
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
