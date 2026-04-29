#!/usr/bin/env python3
"""
Hermes Vibes Trader — Hyperliquid execution layer

This script:
  • Loads current state from live/vibes_positions.json
  • Connects to Hyperliquid testnet (or mainnet based on env)
  • Performs the action (ENTER/EXIT/ADD/REDUCE/REVERSE/HOLD)
  • Updates state file
  • Returns JSON result for logging

All HL interactions are wrapped in try/except. No uncaught exceptions.

Usage:
  python3 scripts/hl_execute_decision.py --decision '{"action":"ENTER",...}' --run-id "20260428_223000"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ─── Hyperliquid SDK imports ──────────────────────────────────────────────────
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

# ─── Config ────────────────────────────────────────────────────────────────────
def _get_env_float(key, default):
    val = os.getenv(key, default)
    # Strip surrounding quotes and whitespace for safety
    if isinstance(val, str):
        val = val.strip().strip('"').strip("'")
    return float(val)

def _get_env_int(key, default):
    val = os.getenv(key, default)
    if isinstance(val, str):
        val = val.strip().strip('"').strip("'")
    return int(val)

BASE_SIZE_BTC = _get_env_float('HERMES_VIBES_BASE_SIZE', '0.01')
LIMIT_OFFSET_BPS = _get_env_int('HERMES_LIMIT_OFFSET_BPS', '5')
USE_TESTNET = os.getenv('USE_TESTNET', 'true').lower() == 'true'
DRY_RUN = os.getenv('HERMES_DRY_RUN', 'false').lower() == 'true'

STATE_FILE = PROJECT_ROOT / 'live' / 'vibes_positions.json'
LOG_FILE = PROJECT_ROOT / 'logs' / 'vibes_executions.jsonl'

# Ensure log dir exists
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


# ─── State helpers ────────────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'BTC': {'in_position': False, 'side': None, 'size': 0.0, 'entry_price': None, 'entry_time': None, 'leverage': 1, 'max_leverage': 40, 'last_action': None, 'last_action_time': None},
        'ETH': {'in_position': False, 'side': None, 'size': 0.0, 'entry_price': None, 'entry_time': None, 'leverage': 1, 'max_leverage': 25, 'last_action': None, 'last_action_time': None},
        'SOL': {'in_position': False, 'side': None, 'size': 0.0, 'entry_price': None, 'entry_time': None, 'leverage': 1, 'max_leverage': 10, 'last_action': None, 'last_action_time': None},
    }

def get_max_leverage(coin: str) -> int:
    """Return max leverage for a coin per HL testnet API."""
    leverage_map = {
        'BTC': 40, 'ETH': 25, 'SOL': 10, 'MATIC': 50,
        'DOGE': 10, 'ARB': 10, 'AVAX': 10, 'BNB': 10, 'LINK': 15,
    }
    return leverage_map.get(coin.upper(), 10)  # default 10x


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def append_execution_log(entry: Dict[str, Any]) -> None:
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')


# ─── HL helpers ───────────────────────────────────────────────────────────────

def get_mid_price(info_client) -> Optional[float]:
    """Get current BTC mid price from HL."""
    try:
        mids = info_client.all_mids()
        if mids and 'BTC' in mids:
            return float(mids['BTC'])
    except Exception as e:
        print(f"[WARN] Could not fetch mid price: {e}", file=sys.stderr)
    return None


def place_limit_order(exchange_client, info_client, side: str, size: float, price: float,
                      leverage: int = 1, coin: str = "BTC") -> Dict[str, Any]:
    """
    Place a GTC limit order on Hyperliquid.

    Args:
        exchange_client: authenticated Exchange client
        info_client: Info client for mid price
        side: 'B' for long (buy), 'A' for short (sell)
        size: order size in BTC/ETH/etc.
        price: limit price in USD
        leverage: leverage multiplier (1-40)
        coin: asset symbol (BTC, ETH, etc.)

    Returns:
        Dict with order status, oid, etc.
    """
    if DRY_RUN:
        return {
            'dry_run': True,
            'side': side,
            'size': size,
            'price': price,
            'leverage': leverage,
            'coin': coin,
            'status': 'simulated',
        }

    # Set leverage first (per-coin)
    try:
        exchange_client.update_leverage(leverage, coin)
    except Exception as e:
        print(f"[WARN] Leverage update failed for {coin}: {e}", file=sys.stderr)

    # Place limit GTC order on Hyperliquid testnet.
    # SDK signature: exchange.order(coin, is_buy, sz, px, order_type, reduce_only, cloid)
    is_buy = (side == 'B')           # 'B' = buy/long → True ;  'A' = sell/short → False
    order_type = {"limit": {"tif": "Gtc"}}
    reduce_only = False
    cloid = None                     # client order ID (optional)
    try:
        order_result = exchange_client.order(coin, is_buy, size, price, order_type, reduce_only, cloid)
        # Surface nested HL error (status: "ok" but statuses[0].error present) as top-level error
        error = None
        resp = order_result.get('response', {})
        data = resp.get('data', {}) if isinstance(resp, dict) else {}
        statuses = data.get('statuses', [])
        if statuses and isinstance(statuses, list):
            first = statuses[0]
            if isinstance(first, dict) and first.get('error'):
                error = first['error']
        return {
            'oid': order_result.get('oid'),
            'status': order_result.get('status'),
            'size': size,
            'price': price,
            'leverage': leverage,
            'coin': coin,
            'raw': order_result,
            'error': error,
        }
    except Exception as e:
        return {
            'error': str(e),
            'status': 'failed',
            'coin': coin,
        }


def close_position(exchange_client, side: str, size: float, price: float, coin: str = "BTC") -> Dict[str, Any]:
    """
    Close a position with a limit order (reduceOnly=True).
    side: actual side of current position ('long' or 'short')
    coin: asset symbol
    """
    if DRY_RUN:
        return {
            'dry_run': True,
            'side': side,
            'size': size,
            'price': price,
            'coin': coin,
            'reduce_only': True,
            'status': 'simulated_close',
        }

    # Close with reduce-only limit GTC.
    # is_buy: to close a LONG we sell (is_buy=False); to close a SHORT we buy (is_buy=True).
    is_buy = (side == 'short')
    order_type = {"limit": {"tif": "Gtc"}}
    try:
        order_result = exchange_client.order(coin, is_buy, size, price, order_type, True, None)
        # Surface nested HL error
        error = None
        resp = order_result.get('response', {})
        data = resp.get('data', {}) if isinstance(resp, dict) else {}
        statuses = data.get('statuses', [])
        if statuses and isinstance(statuses, list):
            first = statuses[0]
            if isinstance(first, dict) and first.get('error'):
                error = first['error']
        return {
            'oid': order_result.get('oid'),
            'status': order_result.get('status'),
            'size': size,
            'price': price,
            'coin': coin,
            'error': error,
            'reduce_only': True,
            'raw': order_result,
        }
    except Exception as e:
        return {
            'error': str(e),
            'status': 'failed',
            'coin': coin,
        }


# ─── Main execution logic ─────────────────────────────────────────────────────

def execute_decision(decision: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """
    Load state → apply decision → place orders → update state → log result.
    Per-asset aware: decision must include 'asset' field (default: BTC).
    """
    result = {
        'run_id': run_id,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'asset': decision.get('asset', 'BTC'),
        'prev_state': None,
        'new_state': None,
        'orders': [],
        'errors': [],
    }
    
    asset = result['asset']
    
    # Load full state (per-asset dict)
    full_state = load_state()
    result['prev_state'] = full_state.get(asset, {}).copy()
    
    # Get or create asset-specific state
    if asset not in full_state:
        full_state[asset] = {
            'in_position': False,
            'side': None,
            'size': 0.0,
            'entry_price': None,
            'entry_time': None,
            'leverage': 1,
            'max_leverage': get_max_leverage(asset),
            'last_action': None,
            'last_action_time': None,
        }
    
    state = full_state[asset]

    # ── Connect to HL ──────────────────────────────────────────────────────────
    secret = os.getenv('HL_SECRET_KEY')
    main_wallet = os.getenv('HL_MAIN_WALLET')
    wallet_api = os.getenv('HL_WALLET_API')

    if not all([secret, main_wallet, wallet_api]):
        result['errors'].append("Missing HL env vars: HL_SECRET_KEY, HL_MAIN_WALLET, HL_WALLET_API")
        append_execution_log(result)
        return result

    base_url = "https://api.hyperliquid-testnet.xyz" if USE_TESTNET else "https://api.hyperliquid.xyz"

    try:
        acct = Account.from_key(secret) if not DRY_RUN else None
        info_client = Info(base_url, skip_ws=True, timeout=15)
        if DRY_RUN:
            exchange_client = None  # No real orders; simulation handled in place_limit_order/close_position
        else:
            exchange_client = Exchange(
                acct,
                base_url,
                account_address=main_wallet,
                timeout=15,
            )
    except Exception as e:
        result['errors'].append(f"HL client init failed: {e}")
        append_execution_log(result)
        return result

    mid_price = get_mid_price(info_client)
    result['mid_price'] = mid_price

    if mid_price is None:
        result['errors'].append('Failed to fetch mid price; aborting')
        append_execution_log(result)
        return result

    # —— Extract decision fields ——
    action = decision.get('action', 'HOLD').upper()
    side = decision.get('side')          # 'long' | 'short' | None
    size = float(decision.get('size', decision.get('size_btc', 0)))
    offset_bps = int(decision.get('limit_offset_bps', LIMIT_OFFSET_BPS))
    leverage = int(decision.get('leverage', 1))

    # Clamp leverage to asset max
    leverage = int(decision.get('leverage', state.get('max_leverage', 10)))
    leverage = max(1, min(state['max_leverage'], leverage))

    # Offset sanity
    if offset_bps < 0:
        offset_bps = 0
    if offset_bps > 100:
        offset_bps = 100

    # Calculate limit price
    if action in ('ENTER', 'ADD'):
        offset = mid_price * (offset_bps / 10000)
        limit_price = mid_price - offset if side == 'long' else mid_price + offset
    elif action in ('REDUCE', 'REVERSE', 'EXIT'):
        offset = mid_price * (offset_bps / 10000)
        if state['side'] == 'long':
            limit_price = mid_price + offset
        elif state['side'] == 'short':
            limit_price = mid_price - offset
        else:
            limit_price = mid_price
    else:  # HOLD
        limit_price = mid_price

    # Round to HL tick size (1 for BTC perps) to avoid "invalid price" errors
    limit_price = round(limit_price)
    result['decision'] = decision
    result['calculated_limit_price'] = limit_price

    # ——— Action router ———

    if action == 'ENTER':
        if state['in_position']:
            result['errors'].append('Already in position — cannot ENTER; use ADD/REVERSE')
        else:
            hl_side = 'B' if side == 'long' else 'A'
            order = place_limit_order(exchange_client, info_client, hl_side, size, limit_price, leverage, coin=asset)
            result['orders'].append(order)
            if order.get('status') in ('open', 'simulated', 'ok') and not order.get('error'):
                state.update({
                    'in_position': True,
                    'side': side,
                    'size': size,
                    'entry_price': limit_price,
                    'entry_time': datetime.utcnow().isoformat() + 'Z',
                    'leverage': leverage,
                    'last_action': 'ENTER',
                    'last_action_time': result['timestamp'],
                })
                # Update full_state
                full_state[asset] = state
            else:
                result['errors'].append(f"ENTER order failed: {order.get('error','unknown')}")

    elif action == 'ADD':
        if not state['in_position']:
            result['errors'].append('Not in position — cannot ADD')
        elif state['side'] != side:
            result['errors'].append(f"Cannot ADD {side}: current side is {state['side']}")
        else:
            new_size = state['size'] + size
            hl_side = 'B' if side == 'long' else 'A'
            order = place_limit_order(exchange_client, info_client, hl_side, size, limit_price, state['leverage'], coin=asset)
            result['orders'].append(order)
            if order.get('status') in ('open', 'simulated', 'ok') and not order.get('error'):
                state['size'] = new_size
                state['last_action'] = 'ADD'
                state['last_action_time'] = result['timestamp']
                full_state[asset] = state
            else:
                result['errors'].append(f"ADD order failed: {order.get('error','unknown')}")

    elif action in ('REDUCE', 'EXIT'):
        if not state['in_position']:
            result['errors'].append('Not in position — nothing to exit')
        else:
            # REDUCE: close half; EXIT: close all
            exit_size = state['size'] / 2 if action == 'REDUCE' else state['size']
            # Use existing leverage
            order = close_position(exchange_client, state['side'], exit_size, limit_price, coin=asset)
            result['orders'].append(order)
            if order.get('status') in ('open', 'simulated', 'filled', 'ok') and not order.get('error'):
                if action == 'EXIT':
                    state.update({
                        'in_position': False,
                        'side': None,
                        'size': 0.0,
                        'entry_price': None,
                        'leverage': 1,
                        'last_action': 'EXIT',
                        'last_action_time': result['timestamp'],
                    })
                else:  # REDUCE
                    state['size'] -= exit_size
                    state['last_action'] = 'REDUCE'
                    state['last_action_time'] = result['timestamp']
                full_state[asset] = state
            else:
                result['errors'].append(f"{action} order failed: {order.get('error','unknown')}")

    elif action == 'REVERSE':
        if not state['in_position']:
            result['errors'].append('Not in position — cannot REVERSE directly')
        else:
            # 1. Close current
            close_order = close_position(exchange_client, state['side'], state['size'], limit_price, coin=asset)
            result['orders'].append({'action': 'close', **close_order})
            if close_order.get('status') not in ('open', 'simulated', 'filled', 'ok') or close_order.get('error'):
                result['errors'].append(f"REVERSE close failed: {close_order.get('error','unknown')}")
            else:
                # State cleared now (simulate immediate for dry-run; production would wait)
                state.update({
                    'in_position': False,
                    'side': None,
                    'size': 0.0,
                    'entry_price': None,
                    'leverage': 1,
                })
                full_state[asset] = state
                # 2. Enter opposite
                time.sleep(0.3)
                hl_side = 'B' if side == 'long' else 'A'
                enter_order = place_limit_order(exchange_client, info_client, hl_side, size, limit_price, leverage, coin=asset)
                result['orders'].append({'action': 'enter', **enter_order})
                if enter_order.get('status') in ('open', 'simulated', 'ok') and not enter_order.get('error'):
                    state.update({
                        'in_position': True,
                        'side': side,
                        'size': size,
                        'entry_price': limit_price,
                        'entry_time': datetime.utcnow().isoformat() + 'Z',
                        'leverage': leverage,
                        'last_action': 'REVERSE',
                        'last_action_time': result['timestamp'],
                    })
                    full_state[asset] = state
                else:
                    result['errors'].append(f"REVERSE enter failed: {enter_order.get('error','unknown')}")

    elif action == 'HOLD':
        state['last_action'] = 'HOLD'
        state['last_action_time'] = result['timestamp']
        full_state[asset] = state
        pass

    else:
        result['errors'].append(f"Unknown action: {action}")

    # ── Finalize ──────────────────────────────────────────────────────────
    save_state(full_state)
    result['new_state'] = state.copy()
    result['status'] = 'success' if not result['errors'] else 'partial_failure'

    append_execution_log(result)
    return result


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Execute a Hermes vibes decision on HL')
    parser.add_argument('--decision', required=True, help='JSON decision string')
    parser.add_argument('--run-id', required=True, help='Unique run identifier for this cycle')
    args = parser.parse_args()

    try:
        decision = json.loads(args.decision)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON decision: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Executing decision: {decision.get('action')} "
          f"{decision.get('side','?')} {decision.get('size', decision.get('size_btc',0))} {asset} "
          f"lev {decision.get('leverage',1)}x "
          f"(offset {decision.get('limit_offset_bps',5)} bps)")

    result = execute_decision(decision, args.run_id)

    # Print summary to stdout for cron logging
    print("=" * 60)
    print(json.dumps(result, indent=2))
    print("=" * 60)

    if result.get('errors'):
        print("ERRORS:", result['errors'], file=sys.stderr)
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
