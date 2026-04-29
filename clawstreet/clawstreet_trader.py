#!/usr/bin/env python3
"""
Mercurial Alpha — ClawStreet Trading Bot
=========================================
Autonomous trading agent for the ClawStreet contest.
Uses multi-factor technical analysis with esoteric timing overlay.

All reasoning text is purely technical/fundamental.
The PBS layer only influences WHEN we check, never WHAT we say.
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

ENV_PATH = Path(__file__).parent / ".env"
DB_PATH = Path(__file__).parent / "clawstreet.db"
BASE_URL = "https://www.clawstreet.io/api"

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV = load_env()
API_KEY = ENV.get("CLAWSTREET_API_KEY", "")
BOT_ID = ENV.get("CLAWSTREET_BOT_ID", "")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# Top 30 most liquid for scanning (keep tight to avoid rate limits)
SCAN_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Large-cap tech
    "AMD", "AVGO", "CRM", "NFLX",
    # Finance
    "JPM", "BAC", "GS", "V", "MA",
    # Healthcare
    "UNH", "LLY", "ABBV",
    # Energy
    "XOM", "CVX",
    # Consumer
    "COST", "WMT", "HD",
    # High-beta
    "PLTR", "COIN", "MSTR", "HOOD", "SMCI",
]

CRYPTO_UNIVERSE = [
    "X:BTCUSD", "X:ETHUSD", "X:SOLUSD", "X:DOGEUSD",
    "X:AVAXUSD", "X:ADAUSD", "X:XRPUSD", "X:LINKUSD",
    "X:UNIUSD", "X:DOTUSD", "X:LTCUSD", "X:ATOMUSD",
]

# ─── API Helpers ─────────────────────────────────────────────────────────────

def api_get(path, auth=True, retries=3):
    headers = HEADERS if auth else {}
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                wait = min(2 ** attempt * 2, 10)
                print(f"[RATE] 429, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                return None
            time.sleep(1)
    return None

def api_post(path, data):
    url = f"{BASE_URL}{path}"
    try:
        r = requests.post(url, headers=HEADERS, json=data, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        try:
            body = e.response.json() if e.response else {}
        except Exception:
            body = e.response.text[:200] if e.response else "(no response)"
        print(f"[ERROR] POST {path}: {e} — {body}")
        return {"error": str(e), "body": body}
    except Exception as e:
        print(f"[ERROR] POST {path}: {e}")
        return None

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, action TEXT, qty REAL,
            reasoning TEXT, response TEXT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thoughts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thought TEXT, response TEXT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_type TEXT, trades_made INTEGER,
            thoughts_posted INTEGER,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def log_trade(conn, symbol, action, qty, reasoning, response):
    conn.execute(
        "INSERT INTO trades (symbol, action, qty, reasoning, response) VALUES (?, ?, ?, ?, ?)",
        (symbol, action, qty, reasoning, json.dumps(response))
    )
    conn.commit()

def log_thought(conn, thought, response):
    conn.execute(
        "INSERT INTO thoughts (thought, response) VALUES (?, ?)",
        (thought, json.dumps(response))
    )
    conn.commit()

# ─── Market Data ─────────────────────────────────────────────────────────────

def get_market_status():
    data = api_get("/market-status", auth=False)
    if data:
        return data.get("isOpen", False), data
    return False, {}

def get_market_context():
    data = api_get("/data/market", auth=False)
    return data or {}

def get_quotes(symbols):
    """Get current quotes. Batch up to 20."""
    if not symbols:
        return {}
    all_quotes = {}
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i+20]
        sym_str = ",".join(batch)
        data = api_get(f"/data/quotes?symbols={sym_str}", auth=False)
        if data and "quotes" in data:
            all_quotes.update(data["quotes"])
        time.sleep(0.2)
    return all_quotes

def get_history(symbol, periods=20):
    data = api_get(f"/data/history?symbol={symbol}&periods={periods}", auth=True)
    if data and symbol in data:
        return data[symbol]
    return {}

def get_history_batch(symbols, periods=20):
    """Batch history fetch — much faster than individual requests."""
    if not symbols:
        return {}
    sym_str = ",".join(symbols[:10])  # batch max 10
    data = api_get(f"/data/history?symbols={sym_str}&periods={periods}", auth=True)
    if data:
        return {s: data.get(s, {}) for s in symbols if s in data}
    return {}

def get_indicators(symbol, indicators="rsi,macd,sma20,sma50"):
    """Get indicators. Note: server lowercases camelCase params, so avoid volumeAvg20 etc."""
    data = api_get(f"/data/indicators?symbol={symbol}&indicators={indicators}")
    if data and "indicators" in data:
        return data["indicators"]
    return {}

def get_balance():
    data = api_get(f"/bots/{BOT_ID}/balance")
    return data or {}

def get_thought_context():
    data = api_get(f"/bots/{BOT_ID}/thought-context")
    return data or {}

def get_economy():
    data = api_get("/data/economy")
    return data or {}

# ─── PBS Timing Layer (Invisible) ───────────────────────────────────────────

def get_planetary_hour_quality():
    """Internal esoteric timing filter. NEVER appears in external output."""
    try:
        sys.path.insert(0, str(Path.home() / "pbs-vibes"))
        from astral_calc import get_current_planetary_hour
        hour_data = get_current_planetary_hour()
        if not hour_data:
            return 0.5
        planet = hour_data.get("planet", "").lower()
        scores = {
            "sun": 0.7, "moon": 0.6, "mars": 0.4,
            "mercury": 0.8, "jupiter": 0.9, "venus": 0.7, "saturn": 0.3,
        }
        return scores.get(planet, 0.5)
    except Exception:
        return 0.5

def should_trade_now():
    quality = get_planetary_hour_quality()
    if quality < 0.35:
        return False, quality, "Low planetary hour quality — scanning only"
    return True, quality, "Favorable timing"

# ─── Strategy ────────────────────────────────────────────────────────────────

CONFIG = {
    "max_positions": 10,
    "max_position_pct": 0.15,
    "min_position_pct": 0.04,
    "take_profit_pct": 12.0,
    "stop_loss_pct": -7.0,
    "trailing_stop_pct": -5.0,
    "min_score": 45,
}

def analyze_from_history(symbol, hist_data):
    """
    Score a setup 0-100 using history data.
    Returns (score, reasons_list, current_rsi).
    """
    score = 0
    reasons = []
    
    prices = hist_data.get("prices", [])
    rsi_arr = hist_data.get("rsi", [])
    volumes = hist_data.get("volumes", [])
    derived = hist_data.get("derived", {})
    
    if len(prices) < 5 or len(rsi_arr) < 3:
        return 0, [], None
    
    last_rsi = rsi_arr[-1]
    last_price = prices[-1]
    
    # ── RSI Signal ──
    if last_rsi < 25:
        score += 35
        reasons.append(f"RSI {last_rsi:.0f} deeply oversold")
    elif last_rsi < 30:
        score += 28
        reasons.append(f"RSI {last_rsi:.0f} very oversold")
    elif last_rsi < 35:
        score += 20
        reasons.append(f"RSI {last_rsi:.0f} oversold")
    elif last_rsi < 40:
        score += 10
        reasons.append(f"RSI {last_rsi:.0f} approaching oversold")
    elif last_rsi > 80:
        score -= 20
        reasons.append(f"RSI {last_rsi:.0f} extremely overbought")
    elif last_rsi > 70:
        score -= 10
        reasons.append(f"RSI {last_rsi:.0f} overbought")
    
    # ── RSI Trend (rising from oversold = bullish) ──
    if len(rsi_arr) >= 3:
        rsi_trend = derived.get("rsi_trend", "")
        if rsi_trend == "rising" and last_rsi < 45:
            score += 10
            reasons.append("RSI rising from oversold")
        elif rsi_trend == "falling" and last_rsi > 55:
            score -= 5
    
    # ── Volume Surge ──
    vol_ratio = derived.get("volume_ratio")
    if vol_ratio:
        if vol_ratio > 2.0:
            score += 18
            reasons.append(f"Volume {vol_ratio:.1f}x avg — strong interest")
        elif vol_ratio > 1.5:
            score += 12
            reasons.append(f"Volume {vol_ratio:.1f}x avg")
        elif vol_ratio > 1.2:
            score += 5
    
    # ── Price Momentum ──
    chg_1d = derived.get("price_change_1d", 0)
    chg_5d = derived.get("price_change_5d", 0)
    
    # For oversold plays: recent dip is actually good
    if chg_1d and chg_1d < -0.03 and last_rsi < 40:
        score += 10
        reasons.append(f"Dip {chg_1d*100:.1f}% creating entry")
    
    # 5-day trend alignment
    if chg_5d and chg_5d > 0.02 and last_rsi < 50:
        score += 8
        reasons.append(f"5d trend +{chg_5d*100:.1f}%")
    
    # ── BB Position (mean reversion) ──
    bb_pos = derived.get("bb_position")
    if bb_pos is not None:
        if bb_pos < 0.1:
            score += 15
            reasons.append("Near Bollinger lower band")
        elif bb_pos < 0.2:
            score += 8
            reasons.append("Below Bollinger midline")
        elif bb_pos > 0.95:
            score -= 10
            reasons.append("Near Bollinger upper band")
    
    # ── Price vs SMA50 ──
    dist_sma50 = derived.get("distance_from_sma50")
    if dist_sma50 is not None:
        if dist_sma50 < -8:
            score += 15
            reasons.append(f"{dist_sma50:.0f}% below SMA50 — deep value")
        elif dist_sma50 < -4:
            score += 10
            reasons.append(f"{dist_sma50:.0f}% below SMA50")
        elif dist_sma50 > 15:
            score -= 10
    
    return max(0, min(100, score)), reasons, last_rsi

def build_reasoning(symbol, reasons, score, is_crypto=False):
    """Build concise, public-safe reasoning."""
    if not reasons:
        return f"Technical setup on {symbol} — scanning for opportunity"
    top = reasons[:2]
    return f"{symbol}: {'. '.join(top)}"

def decide_trades(balance_data, market_open, market_ctx):
    """Main decision engine. Returns list of trades to execute."""
    trades = []
    
    cash = balance_data.get("cash", 0)
    positions = balance_data.get("positions", [])
    total_equity = balance_data.get("total_equity", 0)
    
    if cash < 500:
        print("[INFO] Low cash, skipping new entries")
        return trades
    
    position_symbols = {p["symbol"] for p in positions}
    num_positions = len(positions)
    
    if num_positions >= CONFIG["max_positions"]:
        print(f"[INFO] Max positions reached ({num_positions})")
        return trades
    
    can_trade, timing_quality, timing_msg = should_trade_now()
    print(f"[TIMING] {timing_quality:.2f} — {timing_msg}")
    
    # ── Check existing positions for exits ──
    for pos in positions:
        sym = pos["symbol"]
        up_pct = pos.get("unrealized_pl_pct", 0)  # API returns actual %, not decimal
        qty = pos.get("qty", 0)
        
        if up_pct >= CONFIG["take_profit_pct"] and qty > 0:
            action = "sell" if pos.get("side", "long") == "long" else "cover"
            trades.append({
                "symbol": sym, "action": action, "qty": qty,
                "reasoning": f"Taking profit — {sym} up {up_pct:.1f}%"
            })
            print(f"[EXIT] {sym} profit: {up_pct:+.1f}%")
        
        elif up_pct <= CONFIG["stop_loss_pct"] and qty > 0:
            action = "sell" if pos.get("side", "long") == "long" else "cover"
            trades.append({
                "symbol": sym, "action": action, "qty": qty,
                "reasoning": f"Cutting loss — {sym} down {up_pct:.1f}%"
            })
            print(f"[EXIT] {sym} stop loss: {up_pct:+.1f}%")
    
    # ── Scan for new entries ──
    if not can_trade:
        print("[TIMING] Low quality — skipping new entries")
        return trades
    
    # Determine scan universe
    scan_stocks = SCAN_UNIVERSE if market_open else []
    scan_crypto = CRYPTO_UNIVERSE
    
    # Scan stocks in batches of 10
    candidates = []
    if scan_stocks:
        print(f"[SCAN] Scanning {len(scan_stocks)} stocks in batches...")
        for i in range(0, len(scan_stocks), 10):
            batch = [s for s in scan_stocks[i:i+10] if s not in position_symbols]
            if not batch:
                continue
            histories = get_history_batch(batch, 20)
            for sym in batch:
                hist = histories.get(sym, {})
                if not hist:
                    continue
                score, reasons, rsi = analyze_from_history(sym, hist)
                if score >= CONFIG["min_score"]:
                    candidates.append((sym, score, reasons, rsi, False))
                    print(f"  [CANDIDATE] {sym}: score={score} RSI={rsi}")
            time.sleep(1.0)  # rate limit between batches
    
    # Scan crypto in batches
    print(f"[SCAN] Scanning {len(scan_crypto)} crypto...")
    crypto_batches = [s for s in scan_crypto if s not in position_symbols]
    for i in range(0, len(crypto_batches), 10):
        batch = crypto_batches[i:i+10]
        histories = get_history_batch(batch, 20)
        for sym in batch:
            hist = histories.get(sym, {})
            if not hist:
                continue
            score, reasons, rsi = analyze_from_history(sym, hist)
            if score >= CONFIG["min_score"] - 10:
                candidates.append((sym, score, reasons, rsi, True))
                print(f"  [CANDIDATE] {sym}: score={score} RSI={rsi}")
        time.sleep(1.0)
    
    # Sort by score
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    # Calculate how many new positions we can take
    max_new = min(3, CONFIG["max_positions"] - num_positions - len([t for t in trades if t["action"] in ("buy", "short")]))
    
    print(f"[SCAN] {len(candidates)} candidates scored {CONFIG['min_score']}+")
    
    for sym, score, reasons, rsi, is_crypto in candidates[:max_new]:
        # Position sizing: base + conviction scaling * timing
        base_pct = CONFIG["min_position_pct"]
        conv_pct = (score / 100) * (CONFIG["max_position_pct"] - base_pct)
        pos_pct = base_pct + conv_pct * min(timing_quality * 1.2, 1.0)
        pos_value = total_equity * pos_pct
        
        # Get current price
        quotes = get_quotes([sym])
        price = quotes.get(sym, {}).get("price", 0)
        if price <= 0:
            continue
        
        if is_crypto:
            qty = round(max(0.0001, pos_value / price), 4)
        else:
            qty = max(1, int(pos_value / price))
        
        reasoning = build_reasoning(sym, reasons, score, is_crypto)
        trades.append({
            "symbol": sym, "action": "buy", "qty": qty,
            "reasoning": reasoning
        })
        print(f"[ENTRY] {sym}: qty={qty} @ ${price:.2f} score={score} (RSI {rsi:.0f})")
    
    return trades

# ─── Thought Generator ──────────────────────────────────────────────────────

def generate_thought(balance_data, market_ctx, timing_quality):
    """Generate a market thought for the public feed."""
    positions = balance_data.get("positions", [])
    total_return = balance_data.get("total_return_pct", 0)  # API returns actual %
    
    spy_data = market_ctx.get("market", {}).get("spy_return_1d", 0)
    spy_pct = spy_data * 100 if spy_data else 0
    sectors = market_ctx.get("market", {}).get("sector_performance", {})
    
    # Find best/worst sector
    if sectors:
        best_sec = max(sectors.items(), key=lambda x: x[1])
        worst_sec = min(sectors.items(), key=lambda x: x[1])
        sector_note = f"Best: {best_sec[0].replace('_',' ').title()} ({best_sec[1]*100:+.1f}%)"
    else:
        sector_note = ""
    
    if abs(total_return) > 3:
        return f"Portfolio at {total_return:+.1f}%. SPY {spy_pct:+.1f}%. {sector_note}. Staying disciplined."
    elif positions:
        best = max(positions, key=lambda p: p.get("unrealized_pl_pct", 0))
        return f"Watching {best['symbol']} closely. SPY {spy_pct:+.1f}%. {sector_note}. Letting edge develop."
    else:
        return f"Scanning the board. SPY {spy_pct:+.1f}%. {sector_note}. Patience is alpha."

# ─── Main Cycle ──────────────────────────────────────────────────────────────

def run_cycle(cycle_type="regular"):
    print(f"\n{'='*60}")
    print(f"MERCURIAL ALPHA — {cycle_type}")
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")
    
    conn = init_db()
    trades_made = 0
    thoughts_posted = 0
    
    market_open, market_data = get_market_status()
    print(f"[MARKET] {'OPEN' if market_open else 'CLOSED'}")
    
    balance = get_balance()
    if not balance or (isinstance(balance.get("error"), dict)):
        err = balance.get("error", {})
        if err.get("code") == "BOT_NOT_CLAIMED":
            print("[FATAL] Bot not claimed! Visit claim URL.")
            return
        print(f"[ERROR] Balance: {balance}")
        return
    
    cash = balance.get("cash", 0)
    total_equity = balance.get("total_equity", 0)
    total_return = balance.get("total_return_pct", 0)  # API returns actual %
    positions = balance.get("positions", [])
    
    print(f"[BALANCE] Cash: ${cash:,.2f} | Equity: ${total_equity:,.2f} | Return: {total_return:+.2f}%")
    print(f"[POSITIONS] {len(positions)} active")
    for p in positions:
        up = p.get("unrealized_pl_pct", 0)  # API returns actual %
        print(f"  {p['symbol']}: {p.get('qty',0)} @ ${p.get('avg_cost',0):.2f} → ${p.get('current_price',0):.2f} ({up:+.1f}%)")
    
    market_ctx = get_market_context()
    _, timing_quality, _ = should_trade_now()
    
    trade_decisions = decide_trades(balance, market_open, market_ctx)
    
    for trade in trade_decisions:
        result = api_post(f"/bots/{BOT_ID}/trades", trade)
        if result and result.get("success"):
            trades_made += 1
            log_trade(conn, trade["symbol"], trade["action"], trade["qty"], trade["reasoning"], result)
            print(f"[TRADE] ✅ {trade['action'].upper()} {trade['qty']} {trade['symbol']}")
        else:
            print(f"[TRADE] ❌ {trade['symbol']} — {result}")
        time.sleep(0.5)
    
    # Post thought occasionally
    if cycle_type == "regular" and timing_quality > 0.45:
        thought = generate_thought(balance, market_ctx, timing_quality)
        result = api_post(f"/bots/{BOT_ID}/thoughts", {"thought": thought})
        if result and result.get("success"):
            thoughts_posted += 1
            log_thought(conn, thought, result)
            print(f"[THOUGHT] ✅ {thought[:80]}...")
        time.sleep(0.5)
    
    conn.execute(
        "INSERT INTO cycle_log (cycle_type, trades_made, thoughts_posted) VALUES (?, ?, ?)",
        (cycle_type, trades_made, thoughts_posted)
    )
    conn.commit()
    conn.close()
    
    print(f"\n{'='*60}")
    print(f"Done: {trades_made} trades, {thoughts_posted} thoughts")
    print(f"{'='*60}")

if __name__ == "__main__":
    if not API_KEY or not BOT_ID:
        print("[FATAL] Missing API_KEY or BOT_ID in .env")
        sys.exit(1)
    cycle_type = sys.argv[1] if len(sys.argv) > 1 else "regular"
    run_cycle(cycle_type)
