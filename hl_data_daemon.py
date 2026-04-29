#!/usr/bin/env python3
"""
HL Data Daemon — WebSocket collector with esoteric tagging for PBS Vibes.

Subscribes to trades + asset context for BTC/ETH/SOL on Hyperliquid.
Each trade is tagged with: planetary hour, kill zone, lunar phase, day ruler.
Stores in SQLite (~/pbs-vibes/data/pbs.db) + JSON buffers for cron script.

Usage:
  python3 hl_data_daemon.py                    # testnet
  python3 hl_data_daemon.py --mainnet          # mainnet
"""

import asyncio
import json
import os
import sys
import time
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

# ── Setup ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = DATA_DIR / "daemon.log"
DB_FILE = DATA_DIR / "pbs.db"
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
MAX_TRADES_PER_COIN = 5000
HEALTH_INTERVAL = 60

# ── Network ────────────────────────────────────────────────────────────
USE_TESTNET = True
if "--mainnet" in sys.argv:
    USE_TESTNET = False

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
    if os.environ.get("USE_TESTNET", "true").lower() in ("false", "0", "no"):
        USE_TESTNET = False

WS_URL = "wss://api.hyperliquid-testnet.xyz/ws" if USE_TESTNET else "wss://api.hyperliquid.xyz/ws"
log.info(f"Starting HL Data Daemon — {'TESTNET' if USE_TESTNET else 'MAINNET'}")

# ── Esoteric Engine (inline — no imports needed) ──────────────────────
sys.path.insert(0, str(PROJECT_ROOT))
from engine.planetary_hours import (
    CHALDEAN_ORDER, DAY_RULERS, KILL_ZONES, KZ_TO_LOCATION,
    LOCATIONS, get_active_kill_zone, get_planetary_hour_at_time,
)

SYNODIC_MONTH = 29.530588853
REF_NEW_MOON_JD = 2451550.39

def _dt_to_jd(dt):
    return 2440587.5 + dt.timestamp() / 86400.0

def moon_phase_frac(dt):
    return ((_dt_to_jd(dt) - REF_NEW_MOON_JD) % SYNODIC_MONTH) / SYNODIC_MONTH

PHASE_NAMES = [
    (0.0000, "new_moon"), (0.1250, "waxing_crescent"),
    (0.2500, "first_quarter"), (0.3750, "waxing_gibbous"),
    (0.5000, "full_moon"), (0.6250, "waning_gibbous"),
    (0.7500, "last_quarter"), (0.8750, "waning_crescent"),
]

def moon_phase_name(frac):
    for i in range(len(PHASE_NAMES) - 1, -1, -1):
        if frac >= PHASE_NAMES[i][0]:
            return PHASE_NAMES[i][1]
    return "new_moon"

def get_esoteric_context():
    """Get current esoteric context for tagging data."""
    now = datetime.now(timezone.utc)
    day_ruler = DAY_RULERS[now.weekday()]

    # Active kill zone
    active_kz = get_active_kill_zone(now)
    kz_name = active_kz if active_kz else "none"

    # Planetary hour for active KZ (or Singapore as default)
    planet = "?"
    hour_num = 0
    if active_kz:
        loc_key = KZ_TO_LOCATION.get(active_kz, "singapore")
        ph = get_planetary_hour_at_time(loc_key, now)
        if ph:
            planet = ph["planet"]
            hour_num = ph["hour_number"]
    else:
        ph = get_planetary_hour_at_time("singapore", now)
        if ph:
            planet = ph["planet"]
            hour_num = ph["hour_number"]

    # Lunar
    lunar_frac = moon_phase_frac(now)
    lunar_name = moon_phase_name(lunar_frac)

    return {
        "ts": now.isoformat(),
        "kz": kz_name,
        "planet": planet,
        "hour_num": hour_num,
        "day_ruler": day_ruler,
        "lunar_frac": round(lunar_frac, 4),
        "lunar_name": lunar_name,
    }

# ── SQLite ─────────────────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        ts REAL, coin TEXT, price REAL, volume REAL, side TEXT,
        is_liq INTEGER, notional REAL,
        kz TEXT, planet TEXT, hour_num INTEGER,
        day_ruler TEXT, lunar_frac REAL, lunar_name TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS asset_ctx (
        ts REAL, coin TEXT,
        oracle_px REAL, mark_px REAL, mid_px REAL,
        funding REAL, oi REAL, prev_day_px REAL,
        kz TEXT, planet TEXT, hour_num INTEGER,
        day_ruler TEXT, lunar_frac REAL, lunar_name TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS esoteric_log (
        ts TEXT, kz TEXT, planet TEXT, hour_num INTEGER,
        day_ruler TEXT, lunar_frac REAL, lunar_name TEXT
    )''')

    # Indexes for fast queries
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_kz ON trades(kz)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_planet ON trades(planet)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ctx_ts ON asset_ctx(ts)')

    conn.commit()
    conn.close()
    log.info(f"Database initialized: {DB_FILE}")


def insert_trades(rows):
    """Batch insert trades into SQLite."""
    if not rows:
        return
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()
    c.executemany(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()


def insert_ctx(rows):
    """Batch insert asset context into SQLite."""
    if not rows:
        return
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()
    try:
        c.executemany(
            "INSERT INTO asset_ctx VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows
        )
        conn.commit()
    except Exception as e:
        log.error(f"insert_ctx failed: {e} | row_len={len(rows[0]) if rows else 0} | rows={len(rows)}")
        # Debug: print column count
        c2 = conn.cursor()
        c2.execute("PRAGMA table_info(asset_ctx)")
        cols = c2.fetchall()
        log.error(f"table cols={len(cols)}")
    finally:
        conn.close()


def log_esoteric(eso):
    """Log esoteric state for time-series analysis."""
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()
    c.execute(
        "INSERT INTO esoteric_log VALUES (?,?,?,?,?,?,?)",
        (eso["ts"], eso["kz"], eso["planet"], eso["hour_num"],
         eso["day_ruler"], eso["lunar_frac"], eso["lunar_name"])
    )
    conn.commit()
    conn.close()


# ── State ──────────────────────────────────────────────────────────────
trades_buffer: dict[str, list[dict]] = {c: [] for c in COINS}
asset_ctx: dict[str, dict] = {}
last_msg_time: float = 0
msg_count: int = 0
reconnect_count: int = 0
trade_insert_batch: list = []
ctx_insert_batch: list = []
last_esoteric_ts: str = ""


def save_json_buffers():
    """Write JSON buffers for cron script."""
    try:
        tmp = TRADES_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "trades": trades_buffer,
            }, f)
        tmp.rename(TRADES_FILE)

        tmp2 = CTX_FILE.with_suffix(".tmp")
        with open(tmp2, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "ctx": asset_ctx,
            }, f)
        tmp2.rename(CTX_FILE)
    except Exception as e:
        log.error(f"JSON save failed: {e}")


def save_health():
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
                "db_trades": _count_db_rows("trades"),
                "db_ctx": _count_db_rows("asset_ctx"),
            }, f)
    except Exception:
        pass


def _count_db_rows(table):
    try:
        conn = sqlite3.connect(str(DB_FILE))
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM {table}")
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def process_trade(data: list):
    """Process incoming trade data with esoteric tags."""
    global msg_count, last_msg_time, trade_insert_batch
    last_msg_time = time.time()
    msg_count += 1

    eso = get_esoteric_context()

    # Log esoteric state periodically
    global last_esoteric_ts
    if eso["ts"] != last_esoteric_ts:
        log_esoteric(eso)
        last_esoteric_ts = eso["ts"]

    for trade in data:
        coin = trade.get("coin", "?")
        if coin not in trades_buffer:
            continue

        px = float(trade.get("px", 0))
        sz = float(trade.get("sz", 0))
        side = "buy" if trade.get("side") == "B" else "sell"
        is_liq = trade.get("hash", "") == "0x0000000000000000000000000000000000000000000000000000000000000000"
        ts = trade.get("time", 0) / 1000.0
        notional = px * sz

        # JSON buffer
        entry = {
            "t": trade.get("time", 0),
            "px": px, "sz": sz, "side": side, "liq": is_liq,
            "kz": eso["kz"], "planet": eso["planet"],
            "lunar": eso["lunar_name"],
        }
        trades_buffer[coin].append(entry)
        if len(trades_buffer[coin]) > MAX_TRADES_PER_COIN:
            trades_buffer[coin] = trades_buffer[coin][-MAX_TRADES_PER_COIN:]

        # SQLite batch
        trade_insert_batch.append((
            ts, coin, px, sz, side, int(is_liq), notional,
            eso["kz"], eso["planet"], eso["hour_num"],
            eso["day_ruler"], eso["lunar_frac"], eso["lunar_name"],
        ))

    # Flush batch
    if len(trade_insert_batch) >= 100:
        insert_trades(trade_insert_batch)
        trade_insert_batch.clear()


def process_active_asset_ctx(data: dict):
    """Process asset context with esoteric tags."""
    global last_msg_time, ctx_insert_batch
    last_msg_time = time.time()

    coin = data.get("coin", "?")
    if coin not in COINS:
        return

    ctx = data.get("ctx", {})
    eso = get_esoteric_context()
    now_ts = time.time()

    oracle_px = float(ctx.get("oraclePx", 0))
    mark_px = float(ctx.get("markPx", 0))
    mid_px = float(ctx.get("midPx", 0))
    funding = float(ctx.get("funding", 0))
    oi = float(ctx.get("openInterest", 0))
    prev_day = float(ctx.get("prevDayPx", 0))

    asset_ctx[coin] = {
        "oraclePx": oracle_px, "markPx": mark_px, "midPx": mid_px,
        "funding": funding, "openInterest": oi, "prevDayPx": prev_day,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    ctx_insert_batch.append((
        now_ts, coin, oracle_px, mark_px, mid_px,
        funding, oi, prev_day,
        eso["kz"], eso["planet"], eso["hour_num"],
        eso["day_ruler"], eso["lunar_frac"], eso["lunar_name"],
    ))

    if len(ctx_insert_batch) >= 20:
        insert_ctx(ctx_insert_batch)
        ctx_insert_batch.clear()


async def ws_loop():
    """Main WebSocket loop with reconnection."""
    global reconnect_count
    import websockets

    while True:
        try:
            log.info(f"Connecting to {WS_URL}...")
            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                log.info("Connected. Subscribing...")

                for coin in COINS:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    }))
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "activeAssetCtx", "coin": coin},
                    }))
                    log.info(f"  Subscribed: {coin}")

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
                            log.info(f"Sub confirmed: {data.get('data', {}).get('subscription', {}).get('type', '?')}")
                        elif channel == "error":
                            log.error(f"WS error: {data.get('data', {})}")

                        if msg_count % 100 == 0:
                            save_json_buffers()

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"Process error: {e}")

        except Exception as e:
            reconnect_count += 1
            log.error(f"Disconnected ({e}). Reconnecting in 5s...")
            save_json_buffers()
            save_health()
            if trade_insert_batch:
                insert_trades(trade_insert_batch)
                trade_insert_batch.clear()
            if ctx_insert_batch:
                insert_ctx(ctx_insert_batch)
                ctx_insert_batch.clear()
            await asyncio.sleep(5)


async def periodic_save():
    """Periodically flush everything to disk."""
    while True:
        await asyncio.sleep(30)
        save_json_buffers()
        save_health()
        if trade_insert_batch:
            insert_trades(trade_insert_batch)
            trade_insert_batch.clear()
        if ctx_insert_batch:
            insert_ctx(ctx_insert_batch)
            ctx_insert_batch.clear()
        total = sum(len(v) for v in trades_buffer.values())
        db_trades = _count_db_rows("trades")
        log.info(f"Flush: {total} buffered, {db_trades} in DB, {msg_count} msgs total")


async def main():
    init_db()
    eso = get_esoteric_context()
    log.info(f"Esoteric: KZ={eso['kz']} Planet={eso['planet']} Lunar={eso['lunar_name']} DayRuler={eso['day_ruler']}")
    await asyncio.gather(
        ws_loop(),
        periodic_save(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        save_json_buffers()
        save_health()
        if trade_insert_batch:
            insert_trades(trade_insert_batch)
        if ctx_insert_batch:
            insert_ctx(ctx_insert_batch)
