#!/usr/bin/env python3
"""
Hyperliquid Orderbook Collector Daemon (Multi-Asset)

Persistent WebSocket listener that subscribes to L2 orderbook updates for multiple assets.
Calculates real-time book imbalance (bid_volume - ask_volume) / (bid_volume + ask_volume)
and writes snapshots to SQLite for other scripts to query.

Run:   python3 scripts/hl_orderbook_collector.py start
Stop:  python3 scripts/hl_orderbook_collector.py stop

Env: HL_ORDERBOOK_COINS=ETH,BTC,MATIC (comma-separated, default: BTC)
     HL_WS_URL=wss://api.hyperliquid-testnet.xyz/ws (override WebSocket URL)
"""

import asyncio
import json
import os
import sys
import time
import sqlite3
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

import websockets

# ── Configuration ─────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "hl_orderbook.db"
PID_PATH = PROJECT_ROOT / "live" / "hl_orderbook_collector.pid"
LOG_PATH = PROJECT_ROOT / "logs" / "hl_orderbook_collector.log"

# Default WebSocket URL (testnet)
WS_URL_ENV = os.environ.get("HL_WS_URL", "")
if WS_URL_ENV:
    MAINNET_WS_URL = WS_URL_ENV
else:
    MAINNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"

# Assets to subscribe (from env, default BTC)
COINS_ENV = os.environ.get("HL_ORDERBOOK_COINS", "BTC")
COINS = [c.strip() for c in COINS_ENV.split(",") if c.strip()]
if not COINS:
    COINS = ["BTC"]

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOG_PATH.parent.mkdir(exist_ok=True)
PID_PATH.parent.mkdir(exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── SQLite Schema ─────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    REAL    NOT NULL,   -- seconds since epoch (UTC)
    coin         TEXT    NOT NULL,
    bid_volume   REAL    NOT NULL,   -- total bid volume (USD)
    ask_volume   REAL    NOT NULL,   -- total ask volume (USD)
    imbalance    REAL    NOT NULL,   -- (bid - ask) / (bid + ask)
    mid_price    REAL    NOT NULL,   -- (best_bid + best_ask) / 2
    spread       REAL    NOT NULL    -- best_ask - best_bid
);

CREATE INDEX IF NOT EXISTS idx_ob_timestamp ON orderbook(timestamp);
CREATE INDEX IF NOT EXISTS idx_ob_coin ON orderbook(coin);
"""

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()

def insert_orderbook(ts: float, coin: str, bid_vol: float, ask_vol: float,
                     mid_price: float, spread: float) -> None:
    """Insert an orderbook snapshot."""
    imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0.0
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute(
            "INSERT INTO orderbook (timestamp, coin, bid_volume, ask_volume, imbalance, mid_price, spread) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, coin, bid_vol, ask_vol, imbalance, mid_price, spread)
        )
        conn.commit()

def query_latest_orderbook(coin: str) -> Optional[dict]:
    """Return the most recent orderbook snapshot for a coin."""
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM orderbook WHERE coin = ? ORDER BY timestamp DESC LIMIT 1",
            (coin,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

# ── WebSocket Subscription ───────────────────────────────────────────────

async def listen_orderbook(coin: str) -> None:
    """Listen to L2 orderbook updates for a single coin."""
    backoff = 1.0
    SUBSCRIBE_MSG = {
        "method": "subscribe",
        "subscription": {
            "type": "l2Book",
            "coin": coin
        }
    }
    
    while True:
        try:
            async with websockets.connect(MAINNET_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                log(f"WebSocket connected → subscribing to {coin} orderbook")
                await ws.send(json.dumps(SUBSCRIBE_MSG))
                backoff = 1.0  # reset on success
                
                while True:
                    msg_text = await ws.recv()
                    try:
                        msg = json.loads(msg_text)
                    except json.JSONDecodeError:
                        continue
                    
                    if not isinstance(msg, dict):
                        continue
                    
                    if msg.get("channel") != "l2Book":
                        continue
                    
                    data = msg.get("data")
                    if not isinstance(data, dict):
                        continue
                    
                    # Parse L2 book
                    levels = data.get("levels", [])
                    if len(levels) < 2:
                        continue
                    
                    bids = levels[0]  # list of [price, size]
                    asks = levels[1]
                    
                    # Calculate total volumes (using USD notional)
                    mid_price_data = data.get("midPx")
                    if mid_price_data:
                        mid_price = float(mid_price_data)
                    else:
                        # Calculate mid from best bid/ask
                        best_bid = float(bids[0][0]) if bids else 0
                        best_ask = float(asks[0][0]) if asks else 0
                        mid_price = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0
                    
                    bid_volume = sum(float(b[0]) * float(b[1]) for b in bids[:10])  # top 10 levels
                    ask_volume = sum(float(a[0]) * float(a[1]) for a in asks[:10])
                    
                    spread = float(asks[0][0]) - float(bids[0][0]) if (bids and asks) else 0
                    
                    ts = time.time()
                    insert_orderbook(ts, coin, bid_volume, ask_volume, mid_price, spread)
                    
        except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
            log(f"WebSocket disconnected for {coin}: {e} — reconnecting in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)
        except Exception as e:
            log(f"Unexpected error for {coin}: {e} — reconnecting in {backoff:.0f}s", "ERROR")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)

async def orderbook_listener() -> None:
    """Launch one orderbook listener per coin concurrently."""
    log(f"Starting orderbook listeners for coins: {', '.join(COINS)}")
    tasks = [listen_orderbook(coin) for coin in COINS]
    await asyncio.gather(*tasks)

# ── Signal Handling ─────────────────────────────────────────────────────

running = True

def handle_signal(signum, frame):
    global running
    log(f"Received signal {signum}, shutting down...")
    running = False

# ── Process Lifecycle ───────────────────────────────────────────────────

def write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()))
    log(f"PID file written: {PID_PATH}")

def remove_pid() -> None:
    if PID_PATH.exists():
        PID_PATH.unlink()
        log("PID file removed")

def start() -> None:
    if PID_PATH.exists():
        pid = int(PID_PATH.read_text().strip())
        log(f"Already running (PID {pid}). Stop first.", "WARN")
        sys.exit(1)
    
    log("Starting HL orderbook collector daemon")
    init_db()
    write_pid()
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    try:
        asyncio.run(orderbook_listener())
    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        remove_pid()
        log("Daemon stopped")

def stop() -> None:
    if not PID_PATH.exists():
        log("No PID file — not running?")
        sys.exit(1)
    
    pid = int(PID_PATH.read_text().strip())
    log(f"Sending SIGTERM to PID {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not PID_PATH.exists():
                break
            time.sleep(0.5)
        if PID_PATH.exists():
            log("PID file still present — process may not have exited", "WARN")
        else:
            log(f"Process {pid} stopped")
    except ProcessLookupError:
        log(f"Process {pid} not found — cleaning up PID file")
        remove_pid()

def status() -> None:
    if PID_PATH.exists():
        pid = PID_PATH.read_text().strip()
        log(f"Running (PID {pid})")
    else:
        log("Not running")

# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: hl_orderbook_collector.py {start|stop|status|run}")
        print("  Env: HL_ORDERBOOK_COINS=ETH,BTC,MATIC (comma-separated, default: BTC)")
        sys.exit(1)
    
    cmd = sys.argv[1].lower()
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "run":
        log("Starting in foreground (supervised mode)")
        init_db()
        write_pid()
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        try:
            asyncio.run(orderbook_listener())
        except KeyboardInterrupt:
            log("Interrupted by user")
        finally:
            remove_pid()
            log("Stopped")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
