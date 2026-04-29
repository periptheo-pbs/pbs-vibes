#!/usr/bin/env python3
"""
PBS Vibe Check
===============
Single self-contained script that calculates ALL data for the PBS Vibes
Trading System and outputs a formatted report to stdout.

Architecture: Script is the telescope, Hermes is the astrologer.
No mechanical scoring. No decide_action(). Pure data output.

Sections:
  1. Header (UTC timestamp, day ruler, prices)
  2. Active KZ planetary hour detail + hermetic correspondences
  3. Kill zone table (all 6 KZs with planetary hour sequences)
  4. Technical indicators per asset (1m EMA 9/21 + RSI, 4h EMA 20/50 + RSI)
  5. Market microstructure per asset (price, OI, funding, delta, trapped, liqs)
  6. Lunar phase (current + next 7 transitions)
  7. Portfolio state (all positions, equity, PnL from HL)
  8. Next KZ countdown

Usage:
  python3 pbs_vibe_check.py
  python3 pbs_vibe_check.py --coins BTC ETH SOL
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytz
import requests

# ── Project imports ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.planetary_hours import (
    CHALDEAN_ORDER,
    DAY_RULERS,
    KILL_ZONES,
    KZ_TO_LOCATION,
    LOCATIONS,
    get_active_kill_zone,
    get_planetary_hour_at_time,
    get_planetary_hours_for_date,
    get_planet_for_kz,
    get_kill_zone_utc_range,
    is_in_kill_zone,
)

# ── Configuration ───────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load .env file into os.environ (simple key=value, no shell expansion)."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value

_load_env()

# HL API routing
USE_TESTNET = os.environ.get("USE_TESTNET", "true").lower() in ("true", "1", "yes")
HL_INFO_URL = (
    "https://api.hyperliquid-testnet.xyz/info"
    if USE_TESTNET
    else "https://api.hyperliquid.xyz/info"
)
HL_WALLET = os.environ.get("HL_MAIN_WALLET", "")

# Assets to track
DEFAULT_COINS = ["BTC", "ETH", "SOL"]

# Timezone mapping for KZ locations
TZ = {k: pytz.timezone(v.timezone) for k, v in LOCATIONS.items()}

# KZ display order
KZ_DISPLAY_ORDER = [
    "singapore",
    "dubai",
    "london",
    "new_york_am",
    "london_close",
    "new_york_pm",
]

# ── Planet Classification ───────────────────────────────────────────────────

BULL_PLANETS = {"Jupiter", "Venus", "Sun"}
BEAR_PLANETS = {"Saturn", "Mars"}
BRIDGE_PLANETS = {"Mercury"}
AMPLIFIER_PLANETS = {"Moon"}

# ── Hermetic Correspondences ────────────────────────────────────────────────

HERMETIC_DATA = {
    "Moon": {
        "sephira": "Yesod", "angel": "Gabriel", "metal": "Silver",
        "color": "White", "day": "Monday", "virtue": "Purity",
        "vice": "Illusion", "tarot": "The Moon",
        "alchemy": "Silver (Argentum)",
    },
    "Sun": {
        "sephira": "Tiphareth", "angel": "Michael", "metal": "Gold",
        "color": "Gold", "day": "Sunday", "virtue": "Generosity",
        "vice": "Pride", "tarot": "The Sun",
        "alchemy": "Gold (Aurum)",
    },
    "Mercury": {
        "sephira": "Hod", "angel": "Raphael", "metal": "Mercury",
        "color": "Magenta", "day": "Wednesday", "virtue": "Intellect",
        "vice": "Deceit", "tarot": "The Magician",
        "alchemy": "Mercury (Quicksilver)",
    },
    "Venus": {
        "sephira": "Netzach", "angel": "Anael", "metal": "Copper",
        "color": "Green", "day": "Friday", "virtue": "Love",
        "vice": "Lust", "tarot": "The Empress",
        "alchemy": "Copper (Cuprum)",
    },
    "Mars": {
        "sephira": "Geburah", "angel": "Khamael", "metal": "Iron",
        "color": "Red", "day": "Tuesday", "virtue": "Courage",
        "vice": "Wrath", "tarot": "The Tower",
        "alchemy": "Iron (Ferrum)",
    },
    "Jupiter": {
        "sephira": "Chesed", "angel": "Tzadqel", "metal": "Tin",
        "color": "Blue", "day": "Thursday", "virtue": "Justice",
        "vice": "Excess", "tarot": "The Wheel of Fortune",
        "alchemy": "Tin (Stannum)",
    },
    "Saturn": {
        "sephira": "Binah", "angel": "Cassiel", "metal": "Lead",
        "color": "Black", "day": "Saturday", "virtue": "Discipline",
        "vice": "Melancholy", "tarot": "The World",
        "alchemy": "Lead (Plumbum)",
    },
}


# ── Lunar Phase (inline — no external dependency) ───────────────────────────

SYNODIC_MONTH = 29.530588853  # days
# Reference new moon: 2000-01-06 18:14 UTC (JD 2451550.26)
REF_NEW_MOON_JD = 2451550.39  # 2000-01-06 18:14 UTC (refined to match known FM of May 2, 2026 02:23 UTC)

PHASE_DISPLAY = {
    "new_moon":        ("\U0001f311", "New Moon"),
    "waxing_crescent": ("\U0001f312", "Waxing Crescent"),
    "first_quarter":   ("\U0001f313", "First Quarter"),
    "waxing_gibbous":  ("\U0001f314", "Waxing Gibbous"),
    "full_moon":       ("\U0001f315", "Full Moon"),
    "waning_gibbous":  ("\U0001f316", "Waning Gibbous"),
    "last_quarter":    ("\U0001f317", "Last Quarter"),
    "waning_crescent": ("\U0001f318", "Waning Crescent"),
}
PHASE_BOUNDARIES = [
    (0.0000, "new_moon"),
    (0.1250, "waxing_crescent"),
    (0.2500, "first_quarter"),
    (0.3750, "waxing_gibbous"),
    (0.5000, "full_moon"),
    (0.6250, "waning_gibbous"),
    (0.7500, "last_quarter"),
    (0.8750, "waning_crescent"),
]


def _datetime_to_jd(dt: datetime.datetime) -> float:
    """Convert a datetime to Julian Day number."""
    epoch_jd = 2440587.5  # JD of 1970-01-01 00:00:00 UTC
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return epoch_jd + dt.timestamp() / 86400.0


def _jd_to_datetime(jd: float) -> datetime.datetime:
    """Convert Julian Day to timezone-aware UTC datetime."""
    epoch_jd = 2440587.5
    return datetime.datetime.fromtimestamp(
        (jd - epoch_jd) * 86400, tz=pytz.UTC
    )


def moon_phase_fraction(now_utc: datetime.datetime) -> float:
    """Return lunar phase as fraction [0, 1): 0.0=new, 0.5=full."""
    jd = _datetime_to_jd(now_utc)
    return ((jd - REF_NEW_MOON_JD) % SYNODIC_MONTH) / SYNODIC_MONTH


def moon_phase_name(fraction: float) -> str:
    """Map phase fraction to bucket name."""
    for i in range(len(PHASE_BOUNDARIES) - 1, -1, -1):
        if fraction >= PHASE_BOUNDARIES[i][0]:
            return PHASE_BOUNDARIES[i][1]
    return "new_moon"


def get_upcoming_lunar_phases(
    now_utc: datetime.datetime, count: int = 7
) -> List[Tuple[datetime.datetime, str, float]]:
    """Return the next `count` lunar phase transitions."""
    now_jd = _datetime_to_jd(now_utc)
    months_elapsed = math.floor((now_jd - REF_NEW_MOON_JD) / SYNODIC_MONTH)

    future: List[Tuple[datetime.datetime, str, float]] = []
    for lookahead in range(0, 24):
        month_start_jd = REF_NEW_MOON_JD + (months_elapsed + lookahead) * SYNODIC_MONTH
        for fraction, name in PHASE_BOUNDARIES:
            phase_jd = month_start_jd + fraction * SYNODIC_MONTH
            phase_dt = _jd_to_datetime(phase_jd)
            future.append((phase_dt, name, fraction))

    future.sort(key=lambda x: x[0])
    return [x for x in future if x[0] > now_utc][:count]


# ── Hyperliquid API ─────────────────────────────────────────────────────────

def _hl_post(payload: dict, timeout: int = 10) -> Optional[dict]:
    """POST to HL info endpoint. Returns parsed JSON or None."""
    try:
        resp = requests.post(HL_INFO_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def hl_all_mids() -> Dict[str, float]:
    """Fetch current mid prices for all coins."""
    data = _hl_post({"type": "allMids"})
    if not isinstance(data, dict):
        return {}
    result = {}
    for k, v in data.items():
        try:
            result[k] = float(v)
        except (ValueError, TypeError):
            pass
    return result


def hl_meta_and_ctx() -> Tuple[List[dict], List[dict]]:
    """Fetch meta (universe) and asset contexts. Returns (universe_list, ctxs_list)."""
    data = _hl_post({"type": "metaAndAssetCtxs"})
    if not isinstance(data, list) or len(data) < 2:
        return [], []
    meta = data[0]
    ctxs = data[1]
    # meta is a dict with key "universe" containing the asset list
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    return universe, ctxs or []


def hl_candles(coin: str, interval: str, count: int) -> List[dict]:
    """Fetch candle snapshot for a coin. Returns list of candle dicts."""
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                   "1h": 3_600_000, "4h": 14_400_000}.get(interval, 60_000)
    now_ms = int(_time.time() * 1000)
    start_ms = max(0, now_ms - count * interval_ms * 2)
    data = _hl_post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": now_ms},
    })
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("candles", [])
    return []


def hl_recent_trades(coin: str, limit: int = 500) -> List["TradeTick"]:
    """Fetch recent trades from daemon buffer (preferred) or REST fallback."""
    trades: List[TradeTick] = []

    # Try daemon buffer first
    buffer_path = PROJECT_ROOT / "data" / "trades_buffer.json"
    if buffer_path.exists():
        try:
            buf = json.loads(buffer_path.read_text())
            coin_trades = buf.get("trades", {}).get(coin, [])
            for t in coin_trades[-limit:]:
                px = t.get("px", 0)
                sz = t.get("sz", 0)
                if px > 0 and sz > 0:
                    trades.append(TradeTick(
                        side=t.get("side", "buy"),
                        price=px,
                        size=sz,
                        timestamp_s=t.get("t", 0) / 1000.0,
                        is_liquidation=t.get("liq", False),
                    ))
            if trades:
                return trades
        except Exception:
            pass

    # Fallback: REST
    data = _hl_post({"type": "recentTrades", "coin": coin})
    if not isinstance(data, list):
        return []
    for t in data[-limit:]:
        try:
            side_raw = t.get("side", "B")
            side = "buy" if side_raw == "B" else "sell"
            price = float(t.get("px", 0))
            size = float(t.get("sz", 0))
            ts = t.get("time", int(_time.time() * 1000)) / 1000.0
            hash_str = t.get("hash", "")
            is_liq = hash_str == "0x0000000000000000000000000000000000000000000000000000000000000000"
            if price > 0 and size > 0:
                trades.append(TradeTick(side=side, price=price, size=size,
                                        timestamp_s=ts, is_liquidation=is_liq))
        except (ValueError, TypeError):
            continue
    return trades


def hl_portfolio() -> Dict[str, object]:
    """Fetch full portfolio: perp equity, spot balance, all positions."""
    result: Dict[str, object] = {"wallet": HL_WALLET, "positions": []}
    if not HL_WALLET:
        return result

    # Perp state
    perp = _hl_post({"type": "clearinghouseState", "user": HL_WALLET})
    if isinstance(perp, dict):
        margin = perp.get("marginSummary", {})
        try:
            result["perp_equity"] = float(margin.get("accountValue", 0))
        except (ValueError, TypeError):
            result["perp_equity"] = 0.0

        positions = []
        for ap in perp.get("assetPositions", []):
            if not isinstance(ap, dict):
                continue
            p = ap.get("position", {})
            if not isinstance(p, dict):
                continue
            try:
                coin = p.get("coin", "?")
                szi = float(p.get("szi", 0))
                if szi == 0:
                    continue
                side = "long" if szi > 0 else "short"
                entry = float(p.get("entryPx", 0))
                lev_val = p.get("leverage", {})
                lev = float(lev_val.get("value", 0)) if isinstance(lev_val, dict) else float(lev_val)
                upnl = float(p.get("unrealizedPnl", 0))
                liq = float(p.get("liquidationPx", 0) or 0)
                positions.append({
                    "coin": coin, "side": side, "size": abs(szi),
                    "entry": entry, "leverage": lev, "upnl": upnl,
                    "liquidation_px": liq,
                })
            except (ValueError, TypeError):
                continue
        result["positions"] = positions

    # Spot state
    spot = _hl_post({"type": "spotClearinghouseState", "user": HL_WALLET})
    if isinstance(spot, dict):
        spot_total = 0.0
        for bal in spot.get("balances", []):
            if isinstance(bal, dict):
                try:
                    total = float(bal.get("total", 0))
                    if total > 0 and "USDC" in bal.get("coin", ""):
                        spot_total += total
                except (ValueError, TypeError):
                    pass
        result["spot_balance"] = spot_total
    else:
        result["spot_balance"] = 0.0

    result["total_equity"] = result.get("perp_equity", 0.0) + result.get("spot_balance", 0.0)
    return result


# ── Trade Tick ───────────────────────────────────────────────────────────────

@dataclass
class TradeTick:
    side: str           # "buy" or "sell"
    price: float
    size: float
    timestamp_s: float
    is_liquidation: bool


# ── Technical Indicators ─────────────────────────────────────────────────────

def _extract_close(candle) -> float:
    """Extract close price from HL candle (dict or list)."""
    if isinstance(candle, dict):
        return float(candle["c"])
    try:
        return float(candle[4])
    except (IndexError, TypeError):
        return float(candle[5])


def ema(series: List[float], period: int) -> Optional[float]:
    """Exponential Moving Average. Returns None if series too short."""
    if len(series) < period:
        return None
    alpha = 2.0 / (period + 1)
    val = sum(series[:period]) / period  # SMA seed
    for price in series[period:]:
        val = price * alpha + val * (1 - alpha)
    return val


def rsi(series: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI. Returns None if series too short."""
    if len(series) < period + 1:
        return None
    changes = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Microstructure ───────────────────────────────────────────────────────────

def cumulative_delta(trades: List[TradeTick], window_min: int) -> float:
    """Net buy volume minus sell volume over the last `window_min` minutes."""
    cutoff = _time.time() - window_min * 60
    delta = 0.0
    for t in trades:
        if t.timestamp_s >= cutoff:
            delta += t.size if t.side == "buy" else -t.size
    return delta


def trapped_traders(trades: List[TradeTick], price: float) -> Dict[str, object]:
    """Count longs trapped above and shorts trapped below current price."""
    trapped_longs = 0
    trapped_shorts = 0
    long_prices: List[float] = []
    short_prices: List[float] = []
    for t in trades:
        if t.side == "buy" and t.price > price:
            trapped_longs += 1
            long_prices.append(t.price)
        elif t.side == "sell" and t.price < price:
            trapped_shorts += 1
            short_prices.append(t.price)
    return {
        "trapped_longs": trapped_longs,
        "trapped_shorts": trapped_shorts,
        "avg_long": sum(long_prices) / len(long_prices) if long_prices else None,
        "avg_short": sum(short_prices) / len(short_prices) if short_prices else None,
    }


def liquidation_stats(trades: List[TradeTick], window_min: int = 60) -> Dict[str, object]:
    """Analyze liquidations in the recent trade window."""
    cutoff = _time.time() - window_min * 60
    liqs = [t for t in trades if t.is_liquidation and t.timestamp_s >= cutoff]
    long_liqs = [t for t in liqs if t.side == "sell"]  # long liquidation = forced sell
    short_liqs = [t for t in liqs if t.side == "buy"]   # short liquidation = forced buy
    total_notional = sum(t.price * t.size for t in liqs)
    return {
        "total": len(liqs),
        "long_liqs": len(long_liqs),
        "short_liqs": len(short_liqs),
        "notional": total_notional,
    }


def asset_context(coin: str) -> Dict[str, Optional[float]]:
    """Fetch OI, funding, mark price, etc. for a single coin."""
    universe, ctxs = hl_meta_and_ctx()
    for i, asset in enumerate(universe):
        if isinstance(asset, dict) and asset.get("name") == coin:
            if i < len(ctxs) and isinstance(ctxs[i], dict):
                ctx = ctxs[i]
                out: Dict[str, Optional[float]] = {}
                for hl_key, our_key in [
                    ("openInterest", "oi"), ("funding", "funding"),
                    ("markPx", "mark"), ("midPx", "mid"),
                    ("oraclePx", "oracle"), ("prevDayPx", "prev_day"),
                ]:
                    val = ctx.get(hl_key)
                    if val is not None:
                        try:
                            out[our_key] = float(val)
                        except (ValueError, TypeError):
                            pass
                return out
    return {}


# ── Display Helpers ──────────────────────────────────────────────────────────

PLANET_EMOJI = {
    "Saturn": "\u26e1", "Jupiter": "\u2643", "Mars": "\u2642",
    "Sun": "\u2609", "Venus": "\u2640", "Mercury": "\u263f", "Moon": "\u263d",
}


def planet_bias(planet: str) -> str:
    if planet in BULL_PLANETS:
        return " BULL"
    if planet in BEAR_PLANETS:
        return " BEAR"
    if planet in BRIDGE_PLANETS:
        return " BRIDGE"
    if planet in AMPLIFIER_PLANETS:
        return " AMPLIFIER"
    return ""


def weekday_name(wd: int) -> str:
    return ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"][wd]


def fmt_time(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M")


def minutes_fmt(seconds: int) -> str:
    a = abs(seconds)
    if a < 60:
        return f"{a}s"
    return f"{a // 60}m"


# ── Display Sections ─────────────────────────────────────────────────────────

def print_header(now_utc: datetime.datetime, prices: Dict[str, float], coins: List[str]) -> None:
    day_ruler = DAY_RULERS[now_utc.weekday()]
    price_parts = []
    for c in coins:
        p = prices.get(c)
        price_parts.append(f"{c} ${p:,.0f}" if p else f"{c} \u2014")
    price_str = " | ".join(price_parts)

    print("\u2500" * 72)
    print(f"  PBS VIBE CHECK \u2014 {now_utc:%Y-%m-%d %H:%M} UTC")
    print(f"  Day: {weekday_name(now_utc.weekday())}  |  Day Ruler: {PLANET_EMOJI.get(day_ruler, '?')} {day_ruler}")
    print(f"  {price_str}")
    print(f"  Network: {'TESTNET' if USE_TESTNET else 'MAINNET'}")
    print("\u2500" * 72)


def print_active_kz_detail(now_utc: datetime.datetime, active_kz: Optional[str]) -> None:
    if not active_kz:
        print("\n\u2500\u2500 Active Kill Zone \u2500\u2500")
        print("  \u26a0 No active kill zone")
        return

    loc_key = KZ_TO_LOCATION[active_kz]
    kz_def = KILL_ZONES[active_kz]
    ph = get_planetary_hour_at_time(loc_key, now_utc)
    if ph is None:
        print(f"\n\u2500\u2500 Active Kill Zone \u2014 {kz_def['label']} \u2500\u2500")
        print("  \u26a0 Could not determine planetary hour")
        return

    planet = ph["planet"]
    hour_num = ph["hour_number"]
    is_day = ph["is_day_hour"]
    start_utc = ph["start_utc"]
    end_utc = ph["end_utc"]
    start_local = ph["start_local"]
    end_local = ph["end_local"]
    tz = TZ[loc_key]
    local_now = now_utc.astimezone(tz)

    elapsed = (local_now - start_local).total_seconds()
    duration = (end_local - start_local).total_seconds()
    pct = (elapsed / duration * 100) if duration > 0 else 0
    hour_type = "Day" if is_day else "Night"

    hours = get_planetary_hours_for_date(loc_key, now_utc.date())
    day_h = (hours[11]["end_utc"] - hours[0]["start_utc"]).total_seconds() / 3600
    night_h = 24 - day_h

    bias = planet_bias(planet)
    print(f"\n\u2500\u2500 Current Planetary Hour \u2014 {kz_def['label']} \u2500\u2500")
    print(f"  Planet:        {PLANET_EMOJI.get(planet, '?')} {planet}{bias}")
    print(f"  Hour number:   {hour_num} of 24  ({hour_type} hour)")
    print(f"  Local time:    {fmt_time(start_local)} \u2013 {fmt_time(end_local)} {tz.zone}")
    print(f"  UTC:           {fmt_time(start_utc)} \u2013 {fmt_time(end_utc)}")
    print(f"  Progress:      {pct:.1f}%  ({minutes_fmt(int(elapsed))} elapsed, {minutes_fmt(int(duration - elapsed))} remaining)")
    print(f"  Day length:    {day_h:.2f}h  |  Night length: {night_h:.2f}h")

    hd = HERMETIC_DATA.get(planet)
    if hd:
        print(f"\n  \u26a1 Hermetic Correspondences \u2014 {planet} \u26a1")
        for key in ["sephira", "metal", "virtue", "vice"]:
            print(f"    {key.capitalize():<10} {hd[key]}")


def print_kz_table(now_utc: datetime.datetime, active_kz: Optional[str]) -> None:
    print(f"\n\n{'Kill Zone':<20} {'UTC':<14} {'Planetary Hours'}")
    print("\u2500" * 72)

    for kz_name in KZ_DISPLAY_ORDER:
        kz_def = KILL_ZONES[kz_name]
        label = kz_def["label"]
        utc_range = f"{kz_def['start_hour']:02d}:{kz_def['start_min']:02d}\u2013{kz_def['end_hour']:02d}:{kz_def['end_min']:02d}"
        loc_key = KZ_TO_LOCATION[kz_name]
        start_utc, end_utc = get_kill_zone_utc_range(kz_name, now_utc.date())
        local_start = start_utc.astimezone(TZ[loc_key])
        local_date = local_start.date()
        hours = get_planetary_hours_for_date(loc_key, local_date)
        overlapping = [h for h in hours if h["start_utc"] < end_utc and h["end_utc"] > start_utc]
        overlapping.sort(key=lambda h: h["start_utc"])

        if overlapping:
            seq = " \u2192 ".join(
                f"{PLANET_EMOJI.get(p['planet'], '?')} {p['planet']}" for p in overlapping
            )
        else:
            seq = "\u2014"

        prefix = "\u25c0 " if kz_name == active_kz else "  "
        print(f"{prefix}{label:<18} {utc_range:<14} {seq}")


def print_technicals(coin: str, m1_candles: List[dict], h4_candles: List[dict]) -> Dict[str, Optional[float]]:
    """Print technicals for one coin. Returns dict for use by microstructure."""
    closes_1m = [_extract_close(c) for c in m1_candles]
    e9 = ema(closes_1m, 9)
    e21 = ema(closes_1m, 21)
    r14_1m = rsi(closes_1m, 14)

    closes_4h = [_extract_close(c) for c in h4_candles]
    e20 = ema(closes_4h, 20)
    e50 = ema(closes_4h, 50)
    r14_4h = rsi(closes_4h, 14)

    print(f"\n\n\u2500\u2500 Technical Indicators \u2014 {coin} \u2500\u2500")
    if e9 is not None and e21 is not None:
        e9_a = "\u25b2" if e9 > e21 else ("\u25bc" if e9 < e21 else "\u2500")
        e21_a = "\u25b2" if e21 > e9 else ("\u25bc" if e21 < e9 else "\u2500")
        rsi_s = f"RSI(14) {r14_1m:.1f}" if r14_1m is not None else ""
        print(f"  1m: EMA9 ${e9:,.0f} {e9_a}  EMA21 ${e21:,.0f} {e21_a}  {rsi_s}")
    else:
        print(f"  1m: EMA9 \u2014  EMA21 \u2014")

    if e20 is not None and e50 is not None:
        e20_a = "\u25b2" if e20 > e50 else ("\u25bc" if e20 < e50 else "\u2500")
        e50_a = "\u25b2" if e50 > e20 else ("\u25bc" if e50 < e20 else "\u2500")
        rsi_s = f"RSI(14) {r14_4h:.1f}" if r14_4h is not None else ""
        print(f"  4h: EMA20 ${e20:,.0f} {e20_a}  EMA50 ${e50:,.0f} {e50_a}  {rsi_s}")
    else:
        print(f"  4h: EMA20 \u2014  EMA50 \u2014")

    return {"e20": e20, "e50": e50, "r14_4h": r14_4h, "e9": e9, "e21": e21, "r14_1m": r14_1m}


def print_microstructure(coin: str, trades: List[TradeTick],
                         price: Optional[float], ctx: Dict[str, Optional[float]],
                         tech: Dict[str, Optional[float]]) -> None:
    """Print microstructure data."""
    print(f"\n\u2500\u2500 Microstructure \u2014 {coin} \u2500\u2500")

    oi = ctx.get("oi")
    funding = ctx.get("funding")
    if oi:
        print(f"  OI: {oi:,.0f} {coin}")
    if funding is not None:
        f_ann = funding * 24 * 365 * 100
        print(f"  Funding:    {funding*100:+.6f}%  ({f_ann:+.1f}% ann)")

    if trades:
        print(f"  Delta:")
        for w in [1, 5, 15, 60]:
            cd = cumulative_delta(trades, w)
            sign = "+" if cd >= 0 else ""
            print(f"    {w:>2}m: {sign}{cd:>+8.1f} {coin}")

        if price:
            trap = trapped_traders(trades, price)
            tl, ts = trap["trapped_longs"], trap["trapped_shorts"]
            avg_tl, avg_ts = trap["avg_long"], trap["avg_short"]
            if tl > ts:
                pressure = "\U0001f534 longs trapped \u2192 liq risk \u2193"
            elif ts > tl:
                pressure = "\U0001f7e2 shorts trapped \u2192 squeeze \u2191"
            else:
                pressure = "\u26aa balanced"
            print(f"  Trapped:")
            print(f"    Longs above:  {tl}" + (f"  (avg entry: ${avg_tl:,.0f})" if avg_tl else ""))
            print(f"    Shorts below: {ts}" + (f"  (avg entry: ${avg_ts:,.0f})" if avg_ts else ""))
            print(f"    Pressure:     {pressure}")

        liq = liquidation_stats(trades, 60)
        if liq["total"] > 0:
            rate = liq["total"] / 60
            activity = "\U0001f525 HIGH" if rate >= 2 else ("\u26a0 ELEVATED" if rate >= 1 else "normal")
            print(f"  Liquidations (60m): {liq['total']}  (${liq['notional']:,.0f})  [{activity}]")
            print(f"    Longs rekt:  {liq['long_liqs']}  |  Shorts rekt: {liq['short_liqs']}")
        else:
            print(f"  Liqs (60m): none")


def print_lunar(now_utc: datetime.datetime) -> None:
    frac = moon_phase_fraction(now_utc)
    name = moon_phase_name(frac)
    emoji, display = PHASE_DISPLAY.get(name, ("\U0001f319", name))

    if 0.25 <= frac < 0.75:
        arc = "Bullish arc (FQ \u2192 TQ, bottom at Full Moon)"
    else:
        arc = "Bearish arc (TQ \u2192 FQ, top at New Moon)"

    now_jd = _datetime_to_jd(now_utc)
    months_elapsed = math.floor((now_jd - REF_NEW_MOON_JD) / SYNODIC_MONTH)
    fm_jd = REF_NEW_MOON_JD + (months_elapsed + 0.5) * SYNODIC_MONTH
    nm_jd = REF_NEW_MOON_JD + (months_elapsed + 1.0) * SYNODIC_MONTH
    if fm_jd < now_jd:
        fm_jd += SYNODIC_MONTH
    if nm_jd < now_jd:
        nm_jd += SYNODIC_MONTH
    days_to_fm = (fm_jd - now_jd)
    days_to_nm = (nm_jd - now_jd)

    in_fm_window = days_to_fm <= 1.0

    print(f"\n\n\u2500\u2500 Lunar Phases \u2500\u2500")
    print(f"  Current:     {emoji} {display}")
    print(f"  Arc:         {arc}")
    print(f"  Full Moon:   {days_to_fm:.1f}d away" + ("  \u26a0 IN FM WINDOW \u2014 tighten stops, size -50%" if in_fm_window else ""))
    print(f"  New Moon:    {days_to_nm:.1f}d away")

    upcoming = get_upcoming_lunar_phases(now_utc, count=5)
    print(f"  Next transitions:")
    for dt, pname, _ in upcoming:
        pe, pd = PHASE_DISPLAY.get(pname, ("\U0001f319", pname))
        print(f"    {pe} {pd:<22} {dt.strftime('%b %d')}  {dt.strftime('%H:%M UTC')}")


def print_portfolio(portfolio: Dict[str, object], coins: List[str]) -> None:
    perp_eq = portfolio.get("perp_equity", 0.0)
    spot_bal = portfolio.get("spot_balance", 0.0)
    total_eq = portfolio.get("total_equity", 0.0)

    print(f"\n\n\u2500\u2500 Portfolio (HL {'Testnet' if USE_TESTNET else 'Mainnet'}) \u2500\u2500")
    print(f"  Perp Equity:    ${perp_eq:,.2f}")
    print(f"  Spot Balance:   ${spot_bal:,.2f}")
    print(f"  Total Equity:   ${total_eq:,.2f}")

    positions = portfolio.get("positions", [])
    if positions:
        print(f"  Position: {len(positions)} open")
        for p in positions:
            coin = p["coin"]
            side = p["side"].upper()
            size = p["size"]
            entry = p["entry"]
            lev = p["leverage"]
            upnl = p["upnl"]
            pnl_s = "+" if upnl >= 0 else ""
            liq = p.get("liquidation_px", 0)
            liq_s = f"  liq ${liq:,.0f}" if liq > 0 else ""
            print(f"    {side} {size:.4f} {coin} @ ${entry:,.0f} ({lev:.0f}x)  PnL {pnl_s}${upnl:,.2f}{liq_s}")
    else:
        print(f"  Position: NONE")


def print_next_kz(now_utc: datetime.datetime, active_kz: Optional[str]) -> None:
    """Find and display the next kill zone that hasn't started yet."""
    best_name = None
    best_start = None

    for kz_name in KZ_DISPLAY_ORDER:
        start_utc, end_utc = get_kill_zone_utc_range(kz_name, now_utc.date())
        if kz_name == active_kz:
            continue
        if start_utc > now_utc:
            if best_start is None or start_utc < best_start:
                best_name = kz_name
                best_start = start_utc
        elif end_utc <= now_utc:
            tomorrow_start = start_utc + datetime.timedelta(days=1)
            if best_start is None or tomorrow_start < best_start:
                best_name = kz_name
                best_start = tomorrow_start

    if not best_name or not best_start:
        print(f"\n\u2500\u2500 Next Kill Zone \u2500\u2500\n  (none found)")
        return

    kz_def = KILL_ZONES[best_name]
    loc_key = KZ_TO_LOCATION[best_name]
    local_start = best_start.astimezone(TZ[loc_key])
    delta_s = int((best_start - now_utc).total_seconds())
    planet = get_planet_for_kz(best_start.weekday(), best_start, best_name) or "?"

    print(f"\n\n\u2500\u2500 Next Kill Zone \u2500\u2500")
    print(f"  {kz_def['label']}: starts in {minutes_fmt(delta_s)}")
    print(f"  UTC:   {fmt_time(best_start)}")
    print(f"  Local: {fmt_time(local_start)} ({TZ[loc_key].zone})")
    print(f"  Ruler: {PLANET_EMOJI.get(planet, '?')} {planet}{planet_bias(planet)}")


def print_footer(now_utc: datetime.datetime) -> None:
    pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="PBS Vibe Check — planetary + market data report")
    parser.add_argument("--coins", nargs="+", default=DEFAULT_COINS, help="Assets to track")
    args = parser.parse_args()
    coins = [c.upper() for c in args.coins]

    now_utc = datetime.datetime.now(pytz.UTC)

    # 1. Prices
    prices = hl_all_mids()

    # 2. Active KZ
    active_kz = get_active_kill_zone(now_utc)

    # 3. Header
    print_header(now_utc, prices, coins)

    # 4. Active KZ detail + hermetic
    print_active_kz_detail(now_utc, active_kz)

    # 5. KZ table
    print_kz_table(now_utc, active_kz)

    # 6. Per-asset: technicals + microstructure (combined)
    for coin in coins:
        m1 = hl_candles(coin, "1m", 100)
        h4 = hl_candles(coin, "4h", 80)
        tech = print_technicals(coin, m1, h4)

        trades = hl_recent_trades(coin, limit=500)
        price = prices.get(coin)
        ctx = asset_context(coin)
        print_microstructure(coin, trades, price, ctx, tech)

    # 7. Lunar
    print_lunar(now_utc)

    # 8. Portfolio
    portfolio = hl_portfolio()
    print_portfolio(portfolio, coins)

    # 9. Next KZ
    print_next_kz(now_utc, active_kz)

    # 10. Footer
    print_footer(now_utc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
