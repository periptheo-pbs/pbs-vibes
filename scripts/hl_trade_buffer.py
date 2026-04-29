#!/usr/bin/env python3
"""
Hyperliquid Trade Buffer Daemon

Persistent WebSocket listener that accumulates BTC perpetual swap trades
in a SQLite rolling buffer. Other scripts query this buffer for recent
trade history (instead of relying on the 10-trade REST snapshot).

Run:   python3 scripts/hl_trade_buffer.py start
Stop:  python3 scripts/hl_trade_buffer.py stop
"""

import asyncio
import json
import os
import sys
import time
import sqlite3
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ── Configuration ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "hl_trades.db"
PID_PATH = PROJECT_ROOT / "live" / "hl_trade_buffer.pid"
LOG_PATH = PROJECT_ROOT / "logs" / "hl_trade_buffer.log"

# Rolling buffer: keep last 24 hours of trades (purge older)
RETENTION_SECONDS = 24 * 3600

# WebSocket connection
# Default: testnet (override with HL_WS_URL env)
WS_URL_ENV = os.environ.get("HL_WS_URL", "")
if WS_URL_ENV:
    MAINNET_WS_URL = WS_URL_ENV
else:
    # Default to testnet (user's primary environment)
    MAINNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
# Perpetual swap symbols to subscribe (can be overridden by HL_TRADE_COINS env, comma-separated)
COINS_ENV = os.environ.get("HL_TRADE_COINS", "BTC")
COINS = [c.strip() for c in COINS_ENV.split(",") if c.strip()]
if not COINS:
    COINS = ["BTC"]

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOG_PATH.parent.mkdir(exist_ok=True)
PID_PATH.parent.mkdir(exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── SQLite Schema ─────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    REAL    NOT NULL,   -- seconds since epoch (UTC)
    price        REAL    NOT NULL,
    size         REAL    NOT NULL,   -- BTC
    side         TEXT    NOT NULL,   -- 'B' or 'A'
    notional     REAL    NOT NULL,   -- price * size (USD)
    tid          TEXT,               -- trade ID from HL
    is_liquidation INTEGER DEFAULT 0,
    hash         TEXT,               -- full hash for liquidation detection
    coin         TEXT    DEFAULT 'BTC'  -- asset symbol
);

CREATE INDEX IF NOT EXISTS idx_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_coin ON trades(coin);
"""

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()

def insert_trade(ts: float, price: float, size: float, side: str,
                 tid: str, hash_val: str, coin: str = "BTC") -> None:
    """Insert a single trade into the buffer."""
    notional = price * size
    is_liq = (hash_val == "0x0000000000000000000000000000000000000000000000000000000000000000")
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, price, size, side, notional, tid, is_liquidation, hash, coin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, price, size, side, notional, tid, 1 if is_liq else 0, hash_val, coin)
        )
        conn.commit()

def purge_old_trades(cutoff: float) -> int:
    """Delete trades older than cutoff timestamp. Returns count deleted."""
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        cur = conn.execute("DELETE FROM trades WHERE timestamp < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        return deleted

def query_trades_since(since: float, coin: str = "BTC") -> list[dict]:
    """Return all trades with timestamp >= since for given coin, ordered ascending."""
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT timestamp, price, size, side, notional, is_liquidation FROM trades "
            "WHERE timestamp >= ? AND coin = ? ORDER BY timestamp ASC",
            (since, coin)
        )
        return [dict(row) for row in cur.fetchall()]

# ── WebSocket Subscription ────────────────────────────────────────────────

async def listen_coin(coin: str) -> None:
    """Listen to trades for a single coin, reconnect on failure."""
    backoff = 1.0
    SUBSCRIBE_MSG = {
        "method": "subscribe",
        "subscription": {
            "type": "trades",
            "coin": coin
        }
    }
    while True:
        try:
            async with websockets.connect(MAINNET_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                log(f"WebSocket connected → subscribing to {coin} trades")
                await ws.send(json.dumps(SUBSCRIBE_MSG))
                backoff = 1.0  # reset on success

                while True:
                    msg_text = await ws.recv()
                    try:
                        msg = json.loads(msg_text)
                    except json.JSONDecodeError:
                        continue

                    # Handle both single dict and list of messages
                    msgs = []
                    if isinstance(msg, dict):
                        msgs = [msg]
                    elif isinstance(msg, list):
                        msgs = msg
                    else:
                        continue

                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue

                        if msg.get("channel") != "trades":
                            continue

                        trades = []
                        data_val = msg.get("data")
                        if isinstance(data_val, list):
                            trades = data_val
                        elif isinstance(data_val, dict):
                            trades = data_val.get("trades", [])
                        else:
                            continue

                    for t in trades:
                        try:
                            side = t["side"]
                            price = float(t["px"])
                            size = float(t["sz"])
                            ts_ms = int(t["time"])
                            ts = ts_ms / 1000.0
                            tid = t.get("tid", "")
                            hash_val = t.get("hash", "")

                            insert_trade(ts, price, size, side, tid, hash_val, coin)
                        except (KeyError, ValueError, TypeError) as e:
                            log(f"Failed to parse {coin} trade: {e} — {t}", "WARN")
                            continue

                    # Periodic purge (every ~1000 trades handled)
                    now = time.time()
                    if int(now) % 10 == 0:  # cheap approx every 10s
                        cutoff = now - RETENTION_SECONDS
                        deleted = purge_old_trades(cutoff)
                        if deleted > 0:
                            log(f"Purged {deleted} trades older than {RETENTION_SECONDS/3600:.0f}h")

        except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
            log(f"WebSocket disconnected for {coin}: {e} — reconnecting in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)
        except Exception as e:
            log(f"Unexpected error for {coin}: {e} — reconnecting in {backoff:.0f}s", "ERROR")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)


async def trade_listener() -> None:
    """Launch one listener per coin concurrently."""
    log(f"Starting trade listeners for coins: {', '.join(COINS)}")
    tasks = [listen_coin(coin) for coin in COINS]
    await asyncio.gather(*tasks)

# ── Signal Handling ───────────────────────────────────────────────────────────

running = True

def handle_signal(signum, frame):
    global running
    log(f"Received signal {signum}, shutting down...")
    running = False

# ── Process Lifecycle ─────────────────────────────────────────────────────────

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

    log("Starting HL trade buffer daemon")
    init_db()
    write_pid()

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(trade_listener())
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
        # Wait for removal
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

def query_cli(minutes: int = 60, coin: str = "BTC") -> None:
    """CLI helper: print last N minutes of trades for a coin."""
    since = time.time() - minutes * 60
    trades = query_trades_since(since, coin)
    print(json.dumps(trades, indent=2, default=str))

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: hl_trade_buffer.py {start|stop|status|query N [coin]|run}")
        print("  Env: HL_TRADE_COINS=ETH,BTC,MATIC (comma-separated, default: BTC)")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "query":
        mins = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 60
        coin = sys.argv[3] if len(sys.argv) > 3 else "BTC"
        query_cli(mins, coin)
    elif cmd == "run":
        # Foreground mode for supervised process managers
        log("Starting in foreground (supervised mode)")
        init_db()
        write_pid()
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        try:
            asyncio.run(trade_listener())
        except KeyboardInterrupt:
            log("Interrupted by user")
        finally:
            remove_pid()
            log("Stopped")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
