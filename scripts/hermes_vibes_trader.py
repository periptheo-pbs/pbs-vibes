#!/usr/bin/env python3 -u
"""
Hermes Vibes Trader Daemon
===========================

Watches live/kz_output.txt (populated by cron running kz_calculator.py).
Parses the human-readable output, exercises discretionary vibes judgment,
and executes trades on Hyperliquid testnet.

Architecture:
  cron (every minute): scripts/kz_calculator.py > live/kz_output.txt
  daemon (this script, running continuously): watches file, decides, trades

Position state stored in: live/hermes_vibes_state.json
Action log: logs/hermes_vibes.jsonl
Status log: logs/hermes_vibes_stdout.log

Usage:
  python3 scripts/hermes_vibes_trader.py [--dry-run] [--debug]

Env vars:
  HL_SECRET_KEY       (agent private key, 0x...)
  HL_MAIN_WALLET      (testnet wallet address to trade)
  USE_TESTNET         (default true)
  HERMES_BASE_SIZE_BTC  (default 0.01)
  HERMES_LIMIT_OFFSET_BPS (default 5, i.e., 0.05%)
  HERMES_TIME_EXIT_HOURS (default 4.0)
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

import pytz

# Hyperliquid
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

# ─── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
LIVE_DIR = PROJECT_ROOT / "live"
LOGS_DIR = PROJECT_ROOT / "logs"
KZ_OUTPUT_FILE = LIVE_DIR / "kz_output.txt"
STATE_FILE = LIVE_DIR / "hermes_vibes_state.json"
LOG_FILE = LOGS_DIR / "hermes_vibes.jsonl"
STDOUT_LOG = LOGS_DIR / "hermes_vibes_stdout.log"

# ─── Configuration ────────────────────────────────────────────────────────────
@dataclass
class Config:
    secret_key: str = os.environ.get("HL_SECRET_KEY", "")
    main_wallet: str = os.environ.get("HL_MAIN_WALLET", "")
    use_testnet: bool = os.environ.get("USE_TESTNET", "true").lower() == "true"
    base_size_btc: float = float(os.environ.get("HERMES_BASE_SIZE_BTC", "0.01"))
    limit_offset_bps: float = float(os.environ.get("HERMES_LIMIT_OFFSET_BPS", "5"))
    time_exit_hours: float = float(os.environ.get("HERMES_TIME_EXIT_HOURS", "4.0"))
    dry_run: bool = False
    debug: bool = False

    @property
    def base_url(self) -> str:
        return constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL

    @property
    def market_url(self) -> str:
        return constants.MAINNET_API_URL  # always mainnet for best price data

# ─── Global state ─────────────────────────────────────────────────────────────
cfg = Config()
info_client: Optional[Info] = None
exchange_client: Optional[Exchange] = None
last_seen_updated: Optional[datetime] = None
keep_running = True

# ─── Parsing ───────────────────────────────────────────────────────────────────
def parse_kz_output(text: str) -> Dict[str, Any]:
    """Extract structured data from kz_calculator.py human output."""
    lines = text.splitlines()
    data: Dict[str, Any] = {
        "timestamp": None,
        "updated": None,
        "active_kz": None,
        "planet": None,
        "hour_num": None,
        "hour_type": None,
        "local_time": None,
        "utc_range": None,
        "btc_price": None,
        "ema9_1m": None,
        "ema21_1m": None,
        "rsi_1m": None,
        "ema20_4h": None,
        "ema50_4h": None,
        "rsi_4h": None,
        "funding_rate": None,
        "cum_delta_1m": 0.0,
        "cum_delta_5m": 0.0,
        "cum_delta_15m": 0.0,
        "cum_delta_60m": 0.0,
        "trapped_longs": 0,
        "trapped_shorts": 0,
        "liquidation_total_60m": 0,
        "liquidation_long_60m": 0,
        "liquidation_short_60m": 0,
        "moon_phase": None,
        "moon_fraction": None,
        "is_waxing": None,
        "day_ruler": None,
    }

    # Helper to strip commas from number strings
    def num(s: str) -> float:
        return float(s.replace(',', '').strip())

    for i, line in enumerate(lines):
        stripped = line.strip()
        # ----- Updated timestamp (last lines) -----
        if stripped.startswith("Updated:"):
            m = re.search(r'Updated:\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC', stripped)
            if m:
                data["updated"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                # Also copy to 'timestamp' for convenience
                data["timestamp"] = data["updated"]

        # ----- Header: Day Ruler and BTC price -----
        if "Day Ruler:" in line and "BTC $" in line:
            # Example: "  Day: Tuesday  |  Day Ruler: Mars | BTC $76,285"
            m_day = re.search(r'Day Ruler:\s+(\w+)', line)
            if m_day:
                data["day_ruler"] = m_day.group(1)
            m_btc = re.search(r'BTC\s+\$([\d,]+)', line)
            if m_btc:
                data["btc_price"] = num(m_btc.group(1))

        # ----- Current Planetary Hour -----
        if "Current Planetary Hour" in line:
            # Extract kill zone name from pattern: ── Current Planetary Hour — NY PM Zone ──
            m_kz = re.search(r'Current Planetary Hour — (.+?) Zone', line)
            if m_kz:
                data["active_kz"] = m_kz.group(1).strip()
            # If pattern fails, maybe line itself contains zone; but keep it.

        # Next few lines contain Planet, Hour number, etc.
        # We'll use separate check on line content.
        if stripped.startswith("Planet:"):
            parts = line.split()
            # Typically: "Planet:", symbol, planet_name, maybe emoji
            if len(parts) >= 3:
                # parts[0]="Planet:", parts[1]=symbol, parts[2]=planet
                data["planet"] = parts[2]
        if stripped.startswith("Hour number:"):
            m = re.search(r'Hour number:\s*(\d+)\s+of', stripped)
            if m:
                data["hour_num"] = int(m.group(1))
            # capture day/night
            if "Day hour" in stripped:
                data["hour_type"] = "day"
            elif "Night hour" in stripped:
                data["hour_type"] = "night"
        if stripped.startswith("Local time:"):
            data["local_time"] = stripped.split("Local time:")[1].strip()
        if stripped.startswith("UTC:"):
            data["utc_range"] = stripped.split("UTC:")[1].strip()

        # ----- Technical Indicators -----
        # Lines starting with "  1m:" or "  4h:"
        if stripped.startswith("1m:") or stripped.startswith("4h:"):
            label = stripped.split(":")[0]  # '1m' or '4h'
            # Extract all EMA period/price pairs (commas allowed)
            ema_matches = re.findall(r'EMA(\d+)\s+\$([\d,]+(?:\.\d+)?)', stripped)
            rsi_match = re.search(r'RSI\(14\)\s+([\d.]+)', stripped)
            if label == "1m" and len(ema_matches) >= 2:
                periods = [int(p) for p, _ in ema_matches]
                prices = [num(p) for _, p in ema_matches]
                if 9 in periods and 21 in periods:
                    data["ema9_1m"] = prices[periods.index(9)]
                    data["ema21_1m"] = prices[periods.index(21)]
                if rsi_match:
                    data["rsi_1m"] = float(rsi_match.group(1))
            if label == "4h" and len(ema_matches) >= 2:
                periods = [int(p) for p, _ in ema_matches]
                prices = [num(p) for _, p in ema_matches]
                if 20 in periods and 50 in periods:
                    data["ema20_4h"] = prices[periods.index(20)]
                    data["ema50_4h"] = prices[periods.index(50)]
                if rsi_match:
                    data["rsi_4h"] = float(rsi_match.group(1))

        # ----- Funding rate (optional) -----
        if "Funding Rate:" in line:
            m = re.search(r'Funding Rate:\s+([+\-]?[\d.]+)%', stripped)
            if m:
                data["funding_rate"] = float(m.group(1))

        # ----- Cumulative Delta -----
        # Lines like "      1m: +     0.0 BTC  (— longs)"
        m_delta = re.search(r'(\d+m):\s*([+\-])\s*([\d.]+)\s+BTC', stripped)
        if m_delta:
            t = m_delta.group(1)
            sign = 1 if m_delta.group(2) == '+' else -1
            val = float(m_delta.group(3))
            out_val = sign * val
            if t == "1m":
                data["cum_delta_1m"] = out_val
            elif t == "5m":
                data["cum_delta_5m"] = out_val
            elif t == "15m":
                data["cum_delta_15m"] = out_val
            elif t == "60m":
                data["cum_delta_60m"] = out_val

        # ----- Net Trapped -----
        if stripped.startswith("Longs above:"):
            # e.g., "Longs above:  1263  (avg entry: $76,319)"
            m = re.search(r'Longs above:\s+(\d+)', stripped)
            if m:
                data["trapped_longs"] = int(m.group(1))
        if stripped.startswith("Shorts below:"):
            m = re.search(r'Shorts below:\s+(\d+)', stripped)
            if m:
                data["trapped_shorts"] = int(m.group(1))

        # ----- Liquidations -----
        if "Recent Liquidations (60min):" in line:
            m_total = re.search(r'Recent Liquidations.*?:\s+(\d+)', stripped)
            if m_total:
                data["liquidation_total_60m"] = int(m_total.group(1))
        if stripped.startswith("Longs rekt"):
            m = re.search(r'Longs rekt.*?:\s+(\d+)', stripped)
            if m:
                data["liquidation_long_60m"] = int(m.group(1))
        if stripped.startswith("Shorts rekt"):
            m = re.search(r'Shorts rekt.*?:\s+(\d+)', stripped)
            if m:
                data["liquidation_short_60m"] = int(m.group(1))

        # ----- Lunar Phase -----
        if stripped.startswith("Current:") and "fraction:" in stripped:
            # Example: "  Current:     🌔 Waxing Gibbous          fraction: 0.397"
            # Remove leading emoji(s) and get the text before " fraction"
            # Split at 'fraction:'
            parts = stripped.split("fraction:")
            phase_str = parts[0].strip()  # "🌔 Waxing Gibbous"
            # Remove leading non-alphabetic characters (emoji)
            words = phase_str.split()
            # Filter out non-alphanumeric? Keep letters.
            # Keep words that contain letters; skip emoji
            words_clean = [w for w in words if any(c.isalpha() for c in w)]
            phase_name = " ".join(words_clean).lower()  # "waxing gibbous"
            data["moon_phase"] = phase_name
            if "waxing" in phase_name:
                data["is_waxing"] = True
            elif "waning" in phase_name:
                data["is_waxing"] = False
            # fraction
            data["moon_fraction"] = float(parts[1].strip())

    return data

# ─── Decision Engine (Hermes Vibes) ───────────────────────────────────────────
PLANET_BULL = {"sun", "jupiter", "venus"}
PLANET_BEAR = {"saturn", "moon"}   # Moon is bear
PLANET_NEUTRAL = {"mercury"}
DAY_RULER_BULL = {"Sun", "Jupiter", "Venus"}
DAY_RULER_BEAR = {"Saturn", "Moon"}

def compute_vibe_score(v: Dict[str, Any]) -> float:
    """Deprecated: Vibes-only mode uses qualitative assessment, no numeric scores."""
    return 0.0

def decide_action(v: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """Hermes vibes-only discretionary decision — no numeric scores, pure qualitative feel."""
    in_pos = state.get("in_position", False)
    side = state.get("side")  # "long" or "short"
    entry_time_str = state.get("entry_time")
    entry_time = None
    if entry_time_str:
        entry_time = datetime.fromisoformat(entry_time_str)

    # Vibes-based qualitative assessment (no numbers)
    planet = v.get("planet", "")
    kz = v.get("active_kz", "")
    hour_num = v.get("hour_num", 0)
    btc_price = v.get("btc_price", 0)
    trapped_longs = v.get("trapped_longs", 0)
    trapped_shorts = v.get("trapped_shorts", 0)
    liq_short = v.get("liquidation_short_60m", 0)
    liq_long = v.get("liquidation_long_60m", 0)
    is_waxing = v.get("is_waxing")
    rsi_1m = v.get("rsi_1m", 50)
    delta_5m = v.get("cum_delta_5m", 0)

    # ---- Vibes Commentary (short, 1-2 sentences) ----
    vibes_commentary = f"Vibes: "
    if planet in ("Jupiter", "Sun", "Venus"):
        vibes_commentary += f"{planet} hour feels bullish, "
    elif planet in ("Saturn", "Moon", "Mars"):
        vibes_commentary += f"{planet} hour feels bearish, "
    if trapped_longs > trapped_shorts + 3:
        vibes_commentary += f"trapped longs ({trapped_longs}) suggest downside pressure, "
    elif trapped_shorts > trapped_longs + 3:
        vibes_commentary += f"trapped shorts ({trapped_shorts}) suggest squeeze potential, "
    if liq_short > liq_long * 2:
        vibes_commentary += f"shorts getting rekt ({liq_short} vs {liq_long} long liqs) → bullish fuel, "
    if is_waxing:
        vibes_commentary += "waxing moon building energy, "
    elif is_waxing is False:
        vibes_commentary += "waning moon dissipating, "
    vibes_commentary = vibes_commentary.rstrip(", ") + "."

    # ---- Vibes-driven decisions (no mechanical thresholds) ----
    action = "hold"
    leverage = 1  # default, vibes can override up to aggressive (user preference)
    size_btc = 0.01  # default, vibes can scale
    limit_offset_bps = 5  # default, vibes can widen/tighten

    # Time exit (still mechanical, but user didn't mention removing this)
    if in_pos and entry_time:
        hold_time = datetime.now(timezone.utc) - entry_time
        if hold_time > timedelta(hours=cfg.time_exit_hours):
            action = "exit"
            vibes_commentary += f" | Time exit triggered after {cfg.time_exit_hours}h."

    # Entry/exit/reverse/add based purely on vibes (qualitative)
    if not in_pos:
        # Fresh entry
        if "bull" in vibes_commentary.lower() and "squeeze" in vibes_commentary.lower():
            action = "enter_long"
            leverage = 3
            size_btc = 0.02
            limit_offset_bps = 3
        elif "bear" in vibes_commentary.lower() and "downside" in vibes_commentary.lower():
            action = "enter_short"
            leverage = 2
            size_btc = 0.015
            limit_offset_bps = 4
        elif "bull" in vibes_commentary.lower():
            action = "enter_long"
            leverage = 2
            size_btc = 0.01
            limit_offset_bps = 5
        elif "bear" in vibes_commentary.lower():
            action = "enter_short"
            leverage = 2
            size_btc = 0.01
            limit_offset_bps = 5
    else:
        # Already in position — manage it
        if side == "long":
            if "bear" in vibes_commentary.lower() and "downside" in vibes_commentary.lower():
                # Strong bear vibes → reverse to short
                action = "reverse_to_short"
                leverage = 2
                size_btc = 0.015
            elif "bull" in vibes_commentary.lower() and "squeeze" in vibes_commentary.lower():
                # Strong bull vibes → add to long
                action = "add_long"
                size_btc = state.get("size_btc", 0.01) * 0.5  # add half of current size
                leverage = 3
            elif "bear" in vibes_commentary.lower():
                # Mild bear vibes → reduce long (partial exit)
                action = "reduce_long"
                size_btc = state.get("size_btc", 0.01) * 0.5  # reduce by half
        elif side == "short":
            if "bull" in vibes_commentary.lower() and "squeeze" in vibes_commentary.lower():
                # Strong bull vibes → reverse to long
                action = "reverse_to_long"
                leverage = 3
                size_btc = 0.02
            elif "bear" in vibes_commentary.lower() and "downside" in vibes_commentary.lower():
                # Strong bear vibes → add to short
                action = "add_short"
                size_btc = state.get("size_btc", 0.01) * 0.5
                leverage = 2
            elif "bull" in vibes_commentary.lower():
                # Mild bull vibes → reduce short
                action = "reduce_short"
                size_btc = state.get("size_btc", 0.01) * 0.5

    return {
        "action": action,
        "side": "long" if "long" in action else "short",
        "size_btc": size_btc,
        "leverage": leverage,
        "limit_offset_bps": limit_offset_bps,
        "vibes_commentary": vibes_commentary,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }

# ─── Exchange interaction ──────────────────────────────────────────────────────
def place_limit_order(is_buy: bool, size_btc: float, price: float, reduce_only: bool = False) -> Dict[str, Any]:
    """Place a limit order via Hyperliquid exchange."""
    if cfg.dry_run:
        return {
            "status": "dry-run",
            "action": "limit_order",
            "is_buy": is_buy,
            "size": size_btc,
            "price": price,
            "reduce_only": reduce_only,
        }
    order_type = {"limit": {"tif": "Gtc"}}
    coin = "BTC"
    cloid = None  # could generate deterministic if desired
    try:
        result = exchange_client.order(
            coin,
            is_buy,
            size_btc,
            price,
            order_type,
            reduce_only=reduce_only,
            cloid=cloid,
        )
        return {"status": "submitted", "result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def close_position(is_long: bool, size_btc: float, price: float) -> Dict[str, Any]:
    """Close position with a reduce-only limit order."""
    is_buy = not is_long  # closing long = sell; closing short = buy
    return place_limit_order(is_buy, size_btc, price, reduce_only=True)

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"in_position": False, "side": None, "size_btc": 0.0, "entry_price": None, "entry_time": None, "last_action": None}

def save_state(state: Dict[str, Any]):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def log_action(entry: Dict[str, Any]):
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')

def log_stdout(msg: str):
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    print(line)
    # Also append to stdout log file
    with open(STDOUT_LOG, 'a') as f:
        f.write(line + '\n')

# ─── Reconciliation with exchange state ───────────────────────────────────────
def reconcile_with_exchange() -> Dict[str, Any]:
    """On startup, fetch position from HL and align local state."""
    global info_client
    try:
        user_state = info_client.user_state(cfg.main_wallet)
        # user_state structure: See HL SDK. It contains 'assetPositions' list of positions.
        positions = user_state.get("assetPositions", [])
        btc_pos = None
        for p in positions:
            if p.get("coin") == "BTC":
                btc_pos = p
                break
        if btc_pos:
            pos = btc_pos.get("position", {})
            size = float(pos.get("size", 0))
            if abs(size) > 1e-6:
                side = "long" if size > 0 else "short"
                entry_px = float(pos.get("entryPx", 0))
                return {
                    "in_position": True,
                    "side": side,
                    "size_btc": abs(size),
                    "entry_price": entry_px,
                    "entry_time": None,  # unknown
                    "reconciled": True,
                }
        # No position
        return {"in_position": False, "side": None, "size_btc": 0, "entry_price": None, "entry_time": None, "reconciled": True}
    except Exception as e:
        log_stdout(f"⚠️ Reconciliation failed: {e}")
        return {"in_position": False, "side": None, "size_btc": 0, "entry_price": None, "entry_time": None, "reconciled": False}

# ─── Main loop ─────────────────────────────────────────────────────────────────
def main_loop():
    global last_seen_updated, keep_running
    log_stdout("🚀 Hermes Vibes Trader daemon started")
    log_stdout(f"   Watching: {KZ_OUTPUT_FILE}")
    log_stdout(f"   State: {STATE_FILE}")
    log_stdout(f"   Network: {'TESTNET' if cfg.use_testnet else 'MAINNET'}")
    log_stdout(f"   Wallet: {cfg.main_wallet}")
    log_stdout(f"   Base size: {cfg.base_size_btc} BTC")
    log_stdout(f"   Time exit: {cfg.time_exit_hours} h")
    if cfg.dry_run:
        log_stdout("⚠️  DRY‑RUN mode — no real orders will be placed")

    # Ensure directories
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize HL clients (skip in dry‑run)
    global info_client, exchange_client, wallet
    info_client = None
    exchange_client = None
    wallet = None
    if cfg.dry_run:
        log_stdout("🛠 Dry‑run: HL clients not initialized, wallet check skipped")
    else:
        if not cfg.secret_key or not cfg.main_wallet:
            log_stdout("❌ Missing HL_SECRET_KEY or HL_MAIN_WALLET env vars; exiting.")
            sys.exit(1)
        try:
            wallet = Account.from_key(cfg.secret_key)
            info_client = Info(cfg.base_url, skip_ws=True, timeout=15)
            exchange_client = Exchange(wallet, cfg.base_url, account_address=cfg.main_wallet, timeout=15)
            log_stdout(f"✅ HL clients initialized (address: {wallet.address})")
            # Set leverage (optional)
            try:
                exchange_client.update_leverage(int(os.environ.get("HL_LEVERAGE","1")), "BTC")
                log_stdout("✅ Leverage set")
            except Exception as e:
                log_stdout(f"⚠️  Could not set leverage: {e}")
        except Exception as e:
            log_stdout(f"❌ Failed to initialize HL clients: {e}")
            sys.exit(1)

    # Load or reconcile state
    state = load_state()
    if not state.get("reconciled"):
        if cfg.dry_run:
            log_stdout("🛠 Dry‑run: skipping exchange reconciliation")
            state["reconciled"] = True
            save_state(state)
        else:
            log_stdout("🔄 Reconciling with exchange state...")
            state = reconcile_with_exchange()
            save_state(state)

    poll_interval = 10  # seconds
    last_mtime = 0.0

    while keep_running:
        try:
            if not KZ_OUTPUT_FILE.exists():
                log_stdout(f"⏳ Waiting for {KZ_OUTPUT_FILE.name} to appear...")
                time.sleep(poll_interval)
                continue

            mtime = KZ_OUTPUT_FILE.stat().st_mtime
            if mtime == last_mtime:
                # No change; sleep
                time.sleep(poll_interval)
                continue

            # New output detected
            last_mtime = mtime
            with open(KZ_OUTPUT_FILE) as f:
                text = f.read()

            parsed = parse_kz_output(text)
            parsed_ts = parsed.get("updated")
            if parsed_ts is None:
                log_stdout("⚠️  Could not parse 'Updated' timestamp; skipping")
                continue

            # Prevent double-processing same timestamp (cron may rewrite file?)
            if parsed_ts == last_seen_updated:
                # same content, skip
                time.sleep(1)
                continue

            # Compare to last_seen_updated
            if last_seen_updated and parsed_ts < last_seen_updated:
                # out of order, ignore
                pass
            else:
                last_seen_updated = parsed_ts
                # Decision
                decision = decide_action(parsed, state)
                act = decision["action"]
                side = decision.get("side")
                size = decision.get("size_btc", 0)
                reason = decision.get("reason", "")

                log_stdout(f"📊 Vibes: {reason} | Action: {act}")

                # ----- Execution ─────────────────────────────────────
                if act == "enter_long" or act == "enter_short":
                    current_px = parsed.get("btc_price")
                    if current_px is None:
                        if info_client is not None:
                            try:
                                current_px = info_client.all_mids().get("BTC")
                            except Exception as e:
                                log_stdout(f"⚠️  Could not fetch current price: {e}")
                        if current_px is None:
                            log_stdout("⚠️  No BTC price, cannot place entry order")
                            continue
                    # Use vibes-driven values from decision
                    limit_offset_bps = decision.get("limit_offset_bps", 5)
                    offset = limit_offset_bps / 10000.0
                    size_btc = decision.get("size_btc", 0.01)
                    leverage = decision.get("leverage", 1)
                    # Set leverage dynamically
                    try:
                        exchange_client.update_leverage(leverage, "BTC")
                        log_stdout(f"⚡ Leverage set to {leverage}x")
                    except Exception as e:
                        log_stdout(f"⚠️  Could not set leverage: {e}")
                    if act == "enter_long":
                        limit_px = current_px * (1 - offset)  # buy below mid
                    else:
                        limit_px = current_px * (1 + offset)  # sell above mid
                    limit_px = round(limit_px, 2)
                    vibes_commentary = decision.get("vibes_commentary", "")
                    log_stdout(f"🎸 {vibes_commentary}")
                    order_result = place_limit_order(is_buy=(act=="enter_long"), size_btc=size_btc, price=limit_px)
                    status = order_result.get("status")
                    log_stdout(f"📤 Entry order: {status} | {order_result}")
                    if status in ("submitted", "dry-run"):
                        state.update({
                            "in_position": True,
                            "side": side,
                            "size_btc": size_btc,
                            "entry_price": limit_px,
                            "entry_time": datetime.now(timezone.utc).isoformat(timespec='seconds'),
                            "last_action": act,
                            "leverage": leverage,
                        })
                        save_state(state)

                elif act in ("add_long", "add_short"):
                    if not state.get("in_position"):
                        log_stdout("⚠️ Not in position, cannot add")
                        continue
                    current_px = parsed.get("btc_price")
                    if current_px is None:
                        if info_client is not None:
                            try:
                                current_px = info_client.all_mids().get("BTC")
                            except Exception as e:
                                log_stdout(f"⚠️  Could not fetch current price: {e}")
                        if current_px is None:
                            log_stdout("⚠️  No BTC price available; cannot add")
                            continue
                    # Use vibes-driven values
                    size_btc = decision.get("size_btc", cfg.base_size_btc * 0.5)
                    limit_offset_bps = decision.get("limit_offset_bps", 5)
                    leverage = decision.get("leverage", 1)
                    # Set leverage
                    try:
                        exchange_client.update_leverage(leverage, "BTC")
                    except Exception as e:
                        log_stdout(f"⚠️  Could not set leverage: {e}")
                    offset = limit_offset_bps / 10000.0
                    if act == "add_long":
                        limit_px = current_px * (1 - offset)
                        is_buy = True
                    else:
                        limit_px = current_px * (1 + offset)
                        is_buy = False
                    limit_px = round(limit_px, 2)
                    vibes_commentary = decision.get("vibes_commentary", "")
                    log_stdout(f"🎸 {vibes_commentary}")
                    order_result = place_limit_order(is_buy=is_buy, size_btc=size_btc, price=limit_px, reduce_only=False)
                    log_stdout(f"📤 Add order: {order_result}")
                    if order_result.get("status") in ("submitted", "dry-run"):
                        old_size = state.get("size_btc", 0)
                        old_entry = state.get("entry_price", 0)
                        new_size = old_size + size_btc
                        new_avg = (old_entry * old_size + limit_px * size_btc) / new_size if new_size > 0 else limit_px
                        state.update({
                            "size_btc": new_size,
                            "entry_price": new_avg,
                            "last_action": act,
                        })
                        save_state(state)

                elif act in ("reduce_long", "reduce_short"):
                    if not state.get("in_position"):
                        log_stdout("⚠️ Not in position, cannot reduce")
                        continue
                    current_px = parsed.get("btc_price")
                    if current_px is None:
                        if info_client is not None:
                            try:
                                current_px = info_client.all_mids().get("BTC")
                            except Exception as e:
                                log_stdout(f"⚠️  Could not fetch current price: {e}")
                        if current_px is None:
                            log_stdout("⚠️  No BTC price available; cannot reduce")
                            continue
                    # Vibes-driven reduce size
                    size_btc = decision.get("size_btc", state.get("size_btc", 0.01) * 0.5)
                    limit_offset_bps = decision.get("limit_offset_bps", 5)
                    offset = limit_offset_bps / 10000.0
                    # Reduce long: sell; reduce short: buy
                    is_long = state.get("side") == "long"
                    is_buy = not is_long  # to reduce long, we sell (is_buy=False)
                    if is_buy:
                        limit_px = current_px * (1 - offset)  # buy to cover short
                    else:
                        limit_px = current_px * (1 + offset)  # sell to reduce long
                    limit_px = round(limit_px, 2)
                    vibes_commentary = decision.get("vibes_commentary", "")
                    log_stdout(f"🎸 {vibes_commentary}")
                    # Use reduce_only=True for reduction
                    order_result = place_limit_order(is_buy=is_buy, size_btc=size_btc, price=limit_px, reduce_only=True)
                    log_stdout(f"📤 Reduce order: {order_result}")
                    if order_result.get("status") in ("submitted", "dry-run"):
                        new_size = state.get("size_btc", 0) - size_btc
                        if new_size <= 0.0001:  # effectively flat
                            state.update({"in_position": False, "side": None, "size_btc": 0, "entry_price": None})
                        else:
                            state.update({"size_btc": new_size, "last_action": act})
                        save_state(state)

                elif act in ("exit", "reverse_to_short", "reverse_to_long"):
                    if not state.get("in_position"):
                        log_stdout("⚠️ Not in position, cannot exit")
                        continue
                    current_px = parsed.get("btc_price")
                    if current_px is None:
                        if info_client is not None:
                            try:
                                current_px = info_client.all_mids().get("BTC")
                            except Exception as e:
                                log_stdout(f"⚠️  Could not fetch current price: {e}")
                        if current_px is None:
                            log_stdout("⚠️  No BTC price available; cannot exit")
                            continue
                    # Vibes-driven values
                    limit_offset_bps = decision.get("limit_offset_bps", 5)
                    leverage = decision.get("leverage", 1)
                    size_btc = decision.get("size_btc", state.get("size_btc", 0.01))
                    offset = limit_offset_bps / 10000.0
                    target_side = "short" if "reverse_to_short" in act else "long"
                    is_exit_long = (state.get("side") == "long")  # if we are long and need to exit or reverse to short
                    # Determine order direction: to exit long, we sell; to exit short, we buy
                    is_buy = not is_exit_long
                    # For reverse, we also need to enter opposite side later; but we can close first then maybe open opposite after.
                    limit_px = current_px * (1 - offset) if is_buy else current_px * (1 + offset)
                    limit_px = round(limit_px, 2)
                    close_size = state.get("size_btc", size_btc)  # close entire position
                    order_result = close_position(is_long=is_exit_long, size_btc=close_size, price=limit_px)
                    vibes_commentary = decision.get("vibes_commentary", "")
                    log_stdout(f"🎸 {vibes_commentary}")
                    log_stdout(f"📤 Exit/close order: {order_result}")
                    if order_result.get("status") in ("submitted", "dry-run"):
                        # After close, if reverse, also open opposite
                        if "reverse" in act:
                            # Set leverage for new position
                            try:
                                exchange_client.update_leverage(leverage, "BTC")
                            except Exception as e:
                                log_stdout(f"⚠️  Could not set leverage: {e}")
                            # Determine reverse entry parameters
                            rev_is_buy = not is_exit_long  # opposite of close direction
                            rev_limit_px = current_px * (1 - offset) if rev_is_buy else current_px * (1 + offset)
                            rev_limit_px = round(rev_limit_px, 2)
                            rev_side = "long" if rev_is_buy else "short"
                            rev_order = place_limit_order(is_buy=rev_is_buy, size_btc=size_btc, price=rev_limit_px)
                            log_stdout(f"📤 Reverse entry order: {rev_order}")
                            if rev_order.get("status") in ("submitted", "dry-run"):
                                state.update({
                                    "in_position": True,
                                    "side": rev_side,
                                    "size_btc": size_btc,
                                    "entry_price": rev_limit_px,
                                    "entry_time": datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                    "last_action": act,
                                    "leverage": leverage,
                                })
                        else:
                            # pure exit
                            state.update({"in_position": False, "side": None, "size_btc": 0, "entry_price": None, "last_action": act})
                        save_state(state)

                # Log the structured action
                log_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kz_updated": parsed_ts.isoformat(),
                    "vibes": {k: v for k, v in parsed.items() if v is not None},
                    "state_before": {k: state.get(k) for k in ["in_position","side","size_btc","entry_price"]},
                    "decision": decision,
                }
                log_action(log_entry)

        except Exception as e:
            log_stdout(f"❌ Loop error: {e}")
            import traceback
            traceback.print_exc()
        time.sleep(poll_interval)

def shutdown(signum, frame):
    global keep_running
    log_stdout("🛑 Shutdown signal received")
    keep_running = False

# ─── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Vibes Trader Daemon")
    parser.add_argument("--dry-run", action="store_true", help="Simulate orders without executing")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    cfg.dry_run = args.dry_run
    cfg.debug = args.debug

    # Ensure dirs exist
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Verify env (skip in dry-run)
    if not cfg.dry_run:
        if not cfg.secret_key or not cfg.main_wallet:
            print("❌ Missing HL_SECRET_KEY or HL_MAIN_WALLET env vars")
            sys.exit(1)

    # Signal handlers
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        main_loop()
    except KeyboardInterrupt:
        pass
    finally:
        log_stdout("👋 Daemon stopped")
