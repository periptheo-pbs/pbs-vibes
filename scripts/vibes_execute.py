#!/usr/bin/env python3 -u
"""
Vibes Execute — One-shot trade execution script.
Takes action parameters and executes via Hyperliquid testnet.
Reads/writes state from live/hermes_vibes_state.json.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# ─── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
LIVE_DIR = PROJECT_ROOT / "live"
STATE_FILE = LIVE_DIR / "hermes_vibes_state.json"

# ─── Config from env ──────────────────────────────────────────────────────────
class Config:
    secret_key = os.environ.get("HL_SECRET_KEY", "")
    main_wallet = os.environ.get("HL_MAIN_WALLET", "")
    use_testnet = os.environ.get("USE_TESTNET", "true").lower() == "true"
    
    @property
    def base_url(self):
        from hyperliquid.utils import constants
        return constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL

cfg = Config()

# ─── State helpers ─────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"in_position": False, "side": None, "size_btc": 0, "entry_price": None, "entry_time": None, "leverage": 1}

def save_state(state):
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def log(msg):
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f"[{ts}] {msg}")

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Vibes Execute — one-shot trade execution")
    parser.add_argument("--action", required=True, 
                        choices=["enter_long", "enter_short", "exit", "reverse_to_long", 
                                 "reverse_to_short", "add_long", "add_short", 
                                 "reduce_long", "reduce_short"],
                        help="Action to execute")
    parser.add_argument("--size", type=float, default=0.01, help="Position size in BTC")
    parser.add_argument("--leverage", type=int, default=1, help="Leverage (1-3)")
    parser.add_argument("--bps", type=float, default=5.0, help="Limit offset in basis points")
    parser.add_argument("--price", type=float, help="Current BTC price (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only")
    args = parser.parse_args()

    log(f"🎯 Action: {args.action} | size={args.size} BTC | lev={args.leverage}x | bps={args.bps}")

    # Load state
    state = load_state()
    log(f"📊 State: in_pos={state.get('in_position')} side={state.get('side')} size={state.get('size_btc')}")

    # Init HL clients
    if not args.dry_run:
        if not cfg.secret_key or not cfg.main_wallet:
            log("❌ Missing HL_SECRET_KEY or HL_MAIN_WALLET")
            sys.exit(1)
        try:
            from eth_account import Account
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            
            wallet = Account.from_key(cfg.secret_key)
            info = Info(cfg.base_url, skip_ws=True, timeout=15)
            exchange = Exchange(wallet, cfg.base_url, account_address=cfg.main_wallet, timeout=15)
            
            # Set leverage
            try:
                exchange.update_leverage(args.leverage, "BTC")
                log(f"⚡ Leverage set to {args.leverage}x")
            except Exception as e:
                log(f"⚠️ Leverage warning: {e}")
        except Exception as e:
            log(f"❌ HL init failed: {e}")
            sys.exit(1)
    else:
        info = None
        exchange = None
        log("🛠 DRY-RUN mode")

    # Get BTC price
    current_px = args.price
    if current_px is None and info is not None:
        try:
            current_px = float(info.all_mids().get("BTC"))
            log(f"💰 BTC price: ${current_px:,.2f}")
        except Exception as e:
            log(f"❌ Price fetch failed: {e}")
            sys.exit(1)
    elif current_px is None:
        log("❌ No BTC price provided in dry-run")
        sys.exit(1)

    # Calculate limit price
    offset = args.bps / 10000.0
    is_buy_action = args.action in ("enter_long", "add_long", "reverse_to_long")
    limit_px = round(current_px * (1 - offset) if is_buy_action else current_px * (1 + offset), 2)
    log(f"📍 Limit price: ${limit_px:,.2f}")

    # ─── Execute ────────────────────────────────────────────────────────────
    
    # ENTER LONG/SHORT
    if args.action in ("enter_long", "enter_short"):
        if state.get("in_position"):
            log(f"⚠️ Already in position (side={state.get('side')}), skipping enter")
            sys.exit(0)
        
        if not args.dry_run:
            order = exchange.order(
                name="BTC",
                is_buy=is_buy_action,
                sz=args.size,
                limit_px=limit_px,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=False
            )
            status = order.get("status")
        else:
            order = {"status": "dry-run"}
            status = "dry-run"
        
        log(f"📤 Entry order: {order}")
        if status in ("ok", "dry-run"):
            state.update({
                "in_position": True,
                "side": "long" if is_buy_action else "short",
                "size_btc": args.size,
                "entry_price": limit_px,
                "entry_time": datetime.now(timezone.utc).isoformat(timespec='seconds'),
                "leverage": args.leverage,
                "last_action": args.action,
            })
            save_state(state)
            log("✅ Entry recorded")
        else:
            log(f"❌ Entry failed: {order}")
            sys.exit(1)

    # EXIT / REVERSE
    elif args.action in ("exit", "reverse_to_long", "reverse_to_short"):
        if not state.get("in_position"):
            log("⚠️ Not in position, cannot exit")
            sys.exit(0)
        
        is_long = state.get("side") == "long"
        close_size = state.get("size_btc", args.size)
        
        # Close current position (market order, reduce_only)
        if not args.dry_run:
            close_order = exchange.market_close(
                name="BTC",
                sz=close_size,
                is_long=is_long,
            )
            status = close_order.get("status")
        else:
            close_order = {"status": "dry-run"}
            status = "dry-run"
        
        log(f"📤 Close order: {close_order}")
        
        if status in ("ok", "dry-run"):
            if "reverse" in args.action:
                # Enter opposite
                rev_is_buy = not is_long
                rev_limit_px = round(current_px * (1 - offset) if rev_is_buy else current_px * (1 + offset), 2)
                
                if not args.dry_run:
                    rev_order = exchange.order(
                        name="BTC",
                        is_buy=rev_is_buy,
                        sz=args.size,
                        limit_px=rev_limit_px,
                        order_type={"limit": {"tif": "Gtc"}},
                        reduce_only=False
                    )
                    rev_status = rev_order.get("status")
                else:
                    rev_order = {"status": "dry-run"}
                    rev_status = "dry-run"
                
                log(f"📤 Reverse entry: {rev_order}")
                if rev_status in ("ok", "dry-run"):
                    state.update({
                        "in_position": True,
                        "side": "long" if rev_is_buy else "short",
                        "size_btc": args.size,
                        "entry_price": rev_limit_px,
                        "entry_time": datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        "leverage": args.leverage,
                        "last_action": args.action,
                    })
                    log("✅ Reverse complete")
                else:
                    log(f"❌ Reverse entry failed: {rev_order}")
                    sys.exit(1)
            else:
                state.update({"in_position": False, "side": None, "size_btc": 0, "entry_price": None})
                log("✅ Position closed")
            save_state(state)
        else:
            log(f"❌ Close failed: {close_order}")
            sys.exit(1)

    # ADD LONG/SHORT
    elif args.action in ("add_long", "add_short"):
        if not state.get("in_position"):
            log("⚠️ Not in position, cannot add")
            sys.exit(0)
        if state.get("side") != ("long" if "long" in args.action else "short"):
            log(f"⚠️ Side mismatch: have {state.get('side')}, trying to add {args.action}")
            sys.exit(1)
        
        if not args.dry_run:
            order = exchange.order(
                name="BTC",
                is_buy=is_buy_action,
                sz=args.size,
                limit_px=limit_px,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=False
            )
            status = order.get("status")
        else:
            order = {"status": "dry-run"}
            status = "dry-run"
        
        log(f"📤 Add order: {order}")
        if status in ("ok", "dry-run"):
            old_size = state.get("size_btc", 0)
            old_entry = state.get("entry_price", 0)
            new_size = old_size + args.size
            new_avg = (old_entry * old_size + limit_px * args.size) / new_size if new_size > 0 else limit_px
            state.update({"size_btc": new_size, "entry_price": new_avg, "last_action": args.action})
            save_state(state)
            log("✅ Add recorded")
        else:
            log(f"❌ Add failed: {order}")
            sys.exit(1)

    # REDUCE LONG/SHORT
    elif args.action in ("reduce_long", "reduce_short"):
        if not state.get("in_position"):
            log("⚠️ Not in position, cannot reduce")
            sys.exit(0)
        
        is_long = state.get("side") == "long"
        is_buy_reduce = not is_long  # reduce long = sell (is_buy=False)
        
        if not args.dry_run:
            order = exchange.order(
                name="BTC",
                is_buy=is_buy_reduce,
                sz=args.size,
                limit_px=limit_px,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=True
            )
            status = order.get("status")
        else:
            order = {"status": "dry-run"}
            status = "dry-run"
        
        log(f"📤 Reduce order: {order}")
        if status in ("ok", "dry-run"):
            new_size = state.get("size_btc", 0) - args.size
            if new_size <= 0.0001:
                state.update({"in_position": False, "side": None, "size_btc": 0, "entry_price": None})
                log("✅ Position closed (reduce to zero)")
            else:
                state.update({"size_btc": new_size, "last_action": args.action})
                log(f"✅ Reduced to {new_size} BTC")
            save_state(state)
        else:
            log(f"❌ Reduce failed: {order}")
            sys.exit(1)

    log("🏁 Done")

if __name__ == "__main__":
    main()
