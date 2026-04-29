#!/usr/bin/env python3
"""
HL Data Daemon — Lightweight WebSocket collector for PBS Vibes.

Subscribes to trades + asset context for BTC/ETH/SOL on Hyperliquid.
Writes rolling buffers to ~/pbs-vibes/data/ for the cron script to read.

Usage:
  python3 hl_data_daemon.py                    # testnet
  python3 hl_data_daemon.py --mainnet          # mainnet

Logs to ~/pbs-vibes/data/daemon.log
"""

import asyncio
import json
import os
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

# ── Setup ──────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = DATA_DIR / "daemon.log"
TRADES_FILE = DATA_DIR / "trades_buffer.json"
CTX_FILE = DATA_DIR / "asset_ctx.json"
HEALTH_FILE = DATA_DIR / "daemon_health.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("hl-daemon")

COINS = ["BTC", "ETH", "SOL"]
MAX_TRADES_PER_COIN = 2000  # rolling buffer
HEALTH_INTERVAL = 60  # write health every 60s

# ── Determine network ──────────────────────────────────────────────────
USE_TESTNET = True
if "--mainnet" in sys.argv:
    USE_TESTNET = False

if USE_TESTNET:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("env_loader", str(Path(__file__).resolve().parent.parent / ".env"))
        # Simple .env loader
        env_path = Path(__file__).resolve().parent.parent / ".env"
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
            if os.environ.get("USE_TESTNET", "true").lower() in ("false", "0", "no"):
                USE_TESTNET = False
    except Exception:
        pass

WS_URL = "wss://api.hyperliquid-testnet.xyz/ws" if USE_TESTNET else "wss://api.hyperliquid.xyz/ws"
log.info(f"Starting HL Data Daemon — {'TESTNET' if USE_TESTNET else 'MAINNET'}")
log.info(f"Coins: {COINS}")
log.info(f"Data dir: {DATA_DIR}")

# ── State ──────────────────────────────────────────────────────────────
trades_buffer: dict[str, list[dict]] = {c: [] for c in COINS}
asset_ctx: dict[str, dict] = {}
last_msg_time: float = 0
msg_count: int = 0
reconnect_count: int = 0


def save_trades():
    """Write trades buffer to disk atomically."""
    try:
        tmp = TRADES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "trades": trades_buffer,
            }, f)
        tmp.rename(TRADES_FILE)
    except Exception as e:
        log.error(f"Failed to save trades: {e}")


def save_ctx():
    """Write asset context to disk atomically."""
    try:
        tmp = CTX_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "ctx": asset_ctx,
            }, f)
        tmp.rename(CTX_FILE)
    except Exception as e:
        log.error(f"Failed to save ctx: {e}")


def save_health():
    """Write health status."""
    try:
        with open(HEALTH_FILE, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "running": True,
                "network": "testnet" if USE_TESTNET else "mainnet",
                "coins": COINS,
                "msg_count": msg_count,
                "reconnect_count": reconnect_count,
                "last_msg_age_s": round(time.time() - last_msg_time, 1) if last_msg_time else None,
                "trades_per_coin": {c: len(trades_buffer[c]) for c in COINS},
            }, f)
    except Exception:
        pass


def process_trade(data: list):
    """Process incoming trade data."""
    global msg_count, last_msg_time
    last_msg_time = time.time()
    msg_count += 1

    for trade in data:
        coin = trade.get("coin", "?")
        if coin not in trades_buffer:
            continue

        entry = {
            "t": trade.get("time", 0),
            "px": float(trade.get("px", 0)),
            "sz": float(trade.get("sz", 0)),
            "side": "buy" if trade.get("side") == "B" else "sell",
            "liq": trade.get("hash", "") == "0x0000000000000000000000000000000000000000000000000000000000000000",
        }
        trades_buffer[coin].append(entry)

        # Rolling buffer
        if len(trades_buffer[coin]) > MAX_TRADES_PER_COIN:
            trades_buffer[coin] = trades_buffer[coin][-MAX_TRADES_PER_COIN:]


def process_active_asset_ctx(data: dict):
    """Process active asset context updates."""
    global last_msg_time
    last_msg_time = time.time()

    coin = data.get("coin", "?")
    if coin not in COINS:
        return

    ctx = data.get("ctx", {})
    asset_ctx[coin] = {
        "oraclePx": float(ctx.get("oraclePx", 0)),
        "markPx": float(ctx.get("markPx", 0)),
        "midPx": float(ctx.get("midPx", 0)),
        "funding": float(ctx.get("funding", 0)),
        "openInterest": float(ctx.get("openInterest", 0)),
        "prevDayPx": float(ctx.get("prevDayPx", 0)),
        "updated": datetime.now(timezone.utc).isoformat(),
    }


async def ws_loop():
    """Main WebSocket loop with reconnection."""
    global reconnect_count
    import websockets

    while True:
        try:
            log.info(f"Connecting to {WS_URL}...")
            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                log.info("Connected. Subscribing to feeds...")

                for coin in COINS:
                    # Subscribe to trades
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    }))
                    # Subscribe to active asset context
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "activeAssetCtx", "coin": coin},
                    }))
                    log.info(f"  Subscribed: {coin} trades + ctx")

                save_health()

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        channel = data.get("channel", "")

                        if channel == "trades":
                            process_trade(data.get("data", []))
                        elif channel == "activeAssetCtx":
                            process_active_asset_ctx(data.get("data", {}))
                        elif channel == "subscriptionResponse":
                            log.info(f"Subscription confirmed: {data.get('data', {})}")
                        elif channel == "error":
                            log.error(f"WS error: {data.get('data', {})}")

                        # Periodic saves
                        if msg_count % 50 == 0:
                            save_trades()
                            save_ctx()

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"Message processing error: {e}")

        except Exception as e:
            reconnect_count += 1
            log.error(f"Connection lost ({e}). Reconnecting in 5s...")
            save_trades()
            save_ctx()
            save_health()
            await asyncio.sleep(5)


async def periodic_save():
    """Periodically flush data to disk."""
    while True:
        await asyncio.sleep(30)
        save_trades()
        save_ctx()
        save_health()
        total = sum(len(v) for v in trades_buffer.values())
        log.info(f"Flush: {total} trades buffered, {msg_count} msgs total")


async def main():
    await asyncio.gather(
        ws_loop(),
        periodic_save(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        save_trades()
        save_ctx()
        save_health()
