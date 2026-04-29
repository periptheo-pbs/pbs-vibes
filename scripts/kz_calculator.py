#!/usr/bin/env python3 -u
"""
Kill Zone + Planetary Hour Calculator
======================================

Real-time display of current kill zone status, planetary hour details,
and technical indicators (EMAs, RSI) for BTC perpetuals on Hyperliquid.
Uses astral-based sunrise/sunset calculations per city — no Greenwich
approximations. Designed for quick trading decisions and sanity checks.

Displays:
  1. Current UTC timestamp and day of week
  2. All 6 kill zones with active/upcoming status, time remaining, ruling planet
  3. Live BTC perpetual price from Hyperliquid mainnet
  4. EMA(9,21) on 1-minute chart + RSI(14) on 1-minute
  5. EMA(20,50) on 4-hour chart + RSI(14) on 4-hour
  6. Current lunar phase (fraction) + next 7 lunar phase transitions (dates & UTC times)
  7. Detailed planetary hour breakdown for the currently active kill zone
  8. Next kill zone countdown
  9. Hermetic correspondences for current ruling planet (Sephira, Angel, Metal, Color, Virtue/Vice, Tarot)

Kill Zones (UTC):
  Singapore 23-03 | Dubai 03-07 | London 07-12 | NY AM 12-15
  London Close 15-17 | NY PM 17-23

Planetary rulers follow Chaldean order (Saturn→Jupiter→Mars→Sun→Venus→Mercury→Moon)
repeating continuously. Day starts at local sunrise with that day's ruler.

Usage:
  python3 scripts/kz_calculator.py

Exit code: 0 always (informational only).
"""

from __future__ import annotations

import datetime
import os
import requests
import sys
from typing import Optional, Dict, List, Tuple
import sqlite3

import math

# ─── Additional imports for market microstructure ────────────────────────────
import time as _time
from dataclasses import dataclass

# ─── Additional imports for market microstructure ────────────────────────────
from dataclasses import dataclass

# ─── Project root import ────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import pytz

from engine.planetary_hours import (
    KILL_ZONES,
    KZ_TO_LOCATION,
    DAY_RULERS,
    CHALDEAN_ORDER,
    get_planetary_hours_for_date,
    get_planetary_hour_at_time,
    get_active_kill_zone,
    get_planet_for_kz,
    is_in_kill_zone,
    get_kill_zone_utc_range,
    LOCATIONS,
)

# Hermetic correspondence data (planet → metal, day, etc.)
HERMETIC_DATA = {
    'Moon': {
        'sephira': 'Yesod',
        'angel': 'Gabriel',
        'metal': 'Silver',
        'color': 'White',
        'day': 'Monday',
        'virtue': 'Purity',
        'vice': 'Illusion',
        'tarot': 'The Moon',
        'alchemy': 'Silver (Argentum)',
    },
    'Sun': {
        'sephira': 'Tiphareth',
        'angel': 'Michael',
        'metal': 'Gold',
        'color': 'Gold',
        'day': 'Sunday',
        'virtue': 'Generosity',
        'vice': 'Pride',
        'tarot': 'The Sun',
        'alchemy': 'Gold (Aurum)',
    },
    'Mercury': {
        'sephira': 'Hod',
        'angel': 'Raphael',
        'metal': 'Mercury',
        'color': 'Magenta',
        'day': 'Wednesday',
        'virtue': 'Intellect',
        'vice': 'Deceit',
        'tarot': 'The Magician',
        'alchemy': 'Mercury (Quicksilver)',
    },
    'Venus': {
        'sephira': 'Netzach',
        'angel': 'Anael',
        'metal': 'Copper',
        'color': 'Green',
        'day': 'Friday',
        'virtue': 'Love',
        'vice': 'Lust',
        'tarot': 'The Empress',
        'alchemy': 'Copper (Cuprum)',
    },
    'Mars': {
        'sephira': 'Geburah',
        'angel': 'Khamael',
        'metal': 'Iron',
        'color': 'Red',
        'day': 'Tuesday',
        'virtue': 'Courage',
        'vice': 'Wrath',
        'tarot': 'The Tower',
        'alchemy': 'Iron (Ferrum)',
    },
    'Jupiter': {
        'sephira': 'Chesed',
        'angel': 'Tzadqel',
        'metal': 'Tin',
        'color': 'Blue',
        'day': 'Thursday',
        'virtue': 'Justice',
        'vice': 'Excess',
        'tarot': 'The Wheel of Fortune',
        'alchemy': 'Tin (Stannum)',
    },
    'Saturn': {
        'sephira': 'Binah',
        'angel': 'Cassiel',
        'metal': 'Lead',
        'color': 'Black',
        'day': 'Saturday',
        'virtue': 'Discipline',
        'vice': 'Melancholy',
        'tarot': 'The World',
        'alchemy': 'Lead (Plumbum)',
    },
}

# ─── Skyfield for solar seasons (optional) ────────────────────────────────────
try:
    from skyfield.api import load as _sf_load
    from skyfield import almanac as _sf_almanac
    _SKYFIELD_AVAILABLE = True
except Exception:
    _SKYFIELD_AVAILABLE = False

# ─── Lunar Phase ─────────────────────────────────────────────────────────────────
# Moon phase calculation (synodic month from config; reference new moon JD).
# Phase expressed as fraction ∈ [0, 1): 0.0=new, 0.25=first quarter, 0.5=full, 0.75=last quarter.
from planetary_btc.domain.moon import (
    julian_day as moon_julian_day,
    moon_phase_from_timestamp,
    phase_name as moon_phase_name,
)
from planetary_btc.config import SYNODIC_MONTH, REF_NEW_MOON_JD


# Phase bucket → (emoji, human-readable label)
PHASE_DISPLAY = {
    'new_moon':        ('🌑', 'New Moon'),
    'waxing_crescent': ('🌒', 'Waxing Crescent'),
    'first_quarter':   ('🌓', 'First Quarter'),
    'waxing_gibbous':  ('🌔', 'Waxing Gibbous'),
    'full_moon':       ('🌕', 'Full Moon'),
    'waning_gibbous':  ('🌖', 'Waning Gibbous'),
    'last_quarter':    ('🌗', 'Last Quarter'),
    'waning_crescent': ('🌘', 'Waning Crescent'),
}
PHASE_BOUNDARIES = [
    (0.0000, 'new_moon'),
    (0.1250, 'waxing_crescent'),
    (0.2500, 'first_quarter'),
    (0.3750, 'waxing_gibbous'),
    (0.5000, 'full_moon'),
    (0.6250, 'waning_gibbous'),
    (0.7500, 'last_quarter'),
    (0.8750, 'waning_crescent'),
]

def _jd_to_datetime(jd: float) -> datetime.datetime:
    """Convert a Julian Day number to a timezone‑aware UTC datetime."""
    # Julian Day 0.0 = 4714‑01‑01 BCE 12:00:00 UTC
    # 2440587.5 is JD of 1970‑01‑01 00:00:00 UTC (Unix epoch)
    return datetime.datetime.fromtimestamp((jd - 2440587.5) * 86400,
                                            tz=datetime.timezone.utc)


def get_upcoming_lunar_phases(
    now_utc: datetime.datetime, count: int = 7
) -> List[Tuple[datetime.datetime, str, float]]:
    """
    Return the next ``count`` lunar phase transitions using all 8 phases
    (new moon, waxing crescent, first quarter, waxing gibbous,
    full moon, waning gibbous, last quarter, waning crescent).

    Each entry is (datetime_utc, phase_name, phase_fraction).
    """
    now_jd = moon_julian_day(now_utc.timestamp())
    months_elapsed = math.floor((now_jd - REF_NEW_MOON_JD) / SYNODIC_MONTH)

    future: List[Tuple[datetime.datetime, str, float]] = []
    for lookahead in range(0, 24):  # ~2 years covers worst‑case alignment
        month_start_jd = REF_NEW_MOON_JD + (months_elapsed + lookahead) * SYNODIC_MONTH
        for fraction, name in PHASE_BOUNDARIES:
            phase_jd = month_start_jd + fraction * SYNODIC_MONTH
            phase_dt = _jd_to_datetime(phase_jd)
            future.append((phase_dt, name, fraction))

    future.sort(key=lambda x: x[0])
    return [x for x in future if x[0] > now_utc][:count]


def get_upcoming_seasons(
    now_utc: datetime.datetime, count: int = 4
) -> List[Tuple[datetime.datetime, str]]:
    """
    Return the next ``count`` seasonal markers (vernal equinox, summer solstice,
    autumnal equinox, winter solstice) after ``now_utc``.
    Requires skyfield; gracefully returns [] if unavailable.
    """
    if not _SKYFIELD_AVAILABLE:
        return []
    try:
        ts = _sf_load.timescale()
        eph = _sf_load('de421.bsp')
        t0 = ts.utc(now_utc.year - 1, 1, 1)
        t1 = ts.utc(now_utc.year + 2, 12, 31)
        f = _sf_almanac.seasons(eph)
        times, events = _sf_almanac.find_discrete(t0, t1, f)
        names = ['Vernal Equinox', 'Summer Solstice', 'Autumnal Equinox', 'Winter Solstice']
        upcoming: List[Tuple[datetime.datetime, str]] = []
        for t, ev in zip(times, events):
            dt = t.utc_datetime()
            if dt > now_utc:
                upcoming.append((dt, names[ev]))
        upcoming.sort(key=lambda x: x[0])
        return upcoming[:count]
    except Exception:
        return []


def print_lunar_phases(now_utc: datetime.datetime) -> None:
    """Display current lunar phase and upcoming lunar/solar seasonal markers."""
    print('\n── Lunar Phases ──')

    phase_val = moon_phase_from_timestamp(now_utc.timestamp())
    current_bucket = moon_phase_name(phase_val)
    emoji, display_name = PHASE_DISPLAY.get(current_bucket, ('🌙', current_bucket))
    print(f'  Current:     {emoji} {display_name:<22}  fraction: {phase_val:.3f}')

    upcoming = get_upcoming_lunar_phases(now_utc, count=7)
    print(f'  Next 7 transitions:')
    for dt, bucket, _frac in upcoming:
        emoji_p, name = PHASE_DISPLAY.get(bucket, ('🌙', bucket))
        date_str = dt.strftime('%b %d')
        time_str = dt.strftime('%H:%M UTC')
        print(f'    {emoji_p} {name:<22}  {date_str}  {time_str}')

    # Seasonal markers (equinoxes & solstices)
    seasons = get_upcoming_seasons(now_utc, count=4)
    if seasons:
        print('  Seasonal markers:')
        for dt, sname in seasons:
            if 'Vernal' in sname:
                emoji_s = '🌱'
            elif 'Autumnal' in sname:
                emoji_s = '🍂'
            elif 'Summer' in sname:
                emoji_s = '☀️'
            elif 'Winter' in sname:
                emoji_s = '❄️'
            else:
                emoji_s = '🌙'
            date_str = dt.strftime('%b %d')
            time_str = dt.strftime('%H:%M UTC')
            print(f'    {emoji_s} {sname:<22}  {date_str}  {time_str}')


# ─── Configuration ────────────────────────────────────────────────────────────────
# Bull/Bear planet classification for quick visual reference
BULL_PLANETS = {'Sun', 'Jupiter', 'Venus'}
BEAR_PLANETS = {'Saturn', 'Moon'}
BRIDGE_PLANETS = {'Mercury'}  # neutral, mercurial — not inherently bull or bear
# Timezone mapping for location keys
TZ = {k: pytz.timezone(v.timezone) for k, v in LOCATIONS.items()}

# Kill zone display order (matches table header)
KZ_DISPLAY_ORDER = [
    'singapore',
    'dubai',
    'london',
    'new_york_am',
    'london_close',
    'new_york_pm',
]

# ─── Helper Functions ───────────────────────────────────────────────────────

def fmt_time(dt: datetime.datetime) -> str:
    """Format datetime as HH:MM (no seconds)."""
    return dt.strftime('%H:%M')


def fmt_dt_full(dt: datetime.datetime) -> str:
    """Full datetime for debugging."""
    return dt.strftime('%Y-%m-%d %H:%M:%S %Z')


def planet_bias(planet: str) -> str:
    """Return ' Bull', ' Bear', or ' ⚖️' (Bridge) for alignment."""
    if planet in BULL_PLANETS:
        return ' Bull'
    if planet in BEAR_PLANETS:
        return ' Bear'
    if planet in BRIDGE_PLANETS:
        return ' ⚖️'
    return ''


def get_local_times(location_key: str, utc_start: datetime.datetime,
                    utc_end: datetime.datetime) -> Tuple[datetime.datetime, datetime.datetime]:
    """Convert UTC times to local time for a location."""
    tz = TZ[location_key]
    return utc_start.astimezone(tz), utc_end.astimezone(tz)


def seconds_until(target: datetime.datetime, now: datetime.datetime) -> int:
    """Seconds from now until target (may be negative if past)."""
    return int((target - now).total_seconds())


def minutes_until(seconds: int) -> str:
    """Format seconds as 'Xm' or 'Xs' for short durations."""
    abs_s = abs(seconds)
    if abs_s < 60:
        return f'{abs_s}s'
    return f'{abs_s // 60}m'


def kill_zone_ends_soon(seconds_remaining: int, threshold: int = 3600) -> bool:
    """True if KZ ends within threshold seconds (default 1 hour)."""
    return 0 < seconds_remaining < threshold


def next_kill_zone(now_utc: datetime.datetime) -> Optional[Tuple[str, datetime.datetime]]:
    """Return (kz_name, start_time) of the next kill zone after now."""
    next_kz = None
    next_start = None
    for kz_name in KZ_DISPLAY_ORDER:
        start_utc, end_utc = get_kill_zone_utc_range(kz_name, now_utc.date())
        # If this KZ already started today but hasn't ended, it's current (not next)
        if start_utc <= now_utc < end_utc:
            continue
        # If start is in the future, it's a candidate
        if start_utc > now_utc:
            if next_start is None or start_utc < next_start:
                next_kz = kz_name
                next_start = start_utc
        # Handle KZ that crosses midnight (e.g. Singapore: 23:00-03:00)
        # Its start is yesterday 23:00, end is today 03:00.
        # If now is after end (today 03:00), next occurrence is tonight 23:00.
        if end_utc <= now_utc and start_utc < end_utc:
            # Cross-midnight KZ that ended already today
            tomorrow_start = start_utc + datetime.timedelta(days=1)
            if next_start is None or tomorrow_start < next_start:
                next_kz = kz_name
                next_start = tomorrow_start
    return (next_kz, next_start) if next_kz else None


def planet_emoji(planet: str) -> str:
    """Quick emoji for planet name."""
    emoji_map = {
        'Saturn':  '🪐',
        'Jupiter': '♃',
        'Mars':    '♂',
        'Sun':     '☉',
        'Venus':   '♀',
        'Mercury': '☿',
        'Moon':    '☽',
    }
    return emoji_map.get(planet, '?')


def weekday_name(weekday: int) -> str:
    """Python weekday (0=Mon) → full name."""
    names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
             'Friday', 'Saturday', 'Sunday']
    return names[weekday]


# ─── Hyperliquid Price Fetch ─────────────────────────────────────────────────

def fetch_btc_perp_price(mainnet: bool = True) -> Optional[float]:
    """
    Fetch latest BTC perpetual swap price from Hyperliquid.

    Uses ``candleSnapshot`` for the most recent 1-minute candle.
    Returns the close price as float, or ``None`` on any failure.

    Args:
        mainnet: True → api.hyperliquid.xyz, False → api.hyperliquid-testnet.xyz
    """
    api_url = 'https://api.hyperliquid.xyz/info' if mainnet else 'https://api.hyperliquid-testnet.xyz/info'
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    # Request a short window ending now; last candle gives latest close
    payload = {
        'type': 'candleSnapshot',
        'req': {
            'coin': 'BTC',
            'interval': '1m',
            'startTime': now_ms - 5 * 60 * 1000,   # 5 min ago
            'endTime': now_ms,
        }
    }
    try:
        resp = requests.post(api_url, json=payload, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        # Modern HL API: list of dicts with keys: t, T, s, i, o, c, h, l, v, n
        # Legacy (some endpoints): list-of-lists [ts, o, h, l, c, v]
        if isinstance(data, list) and len(data) > 0:
            last_candle = data[-1]
            # Detect format: dict → 'c' key, list/tuple → index 4
            if isinstance(last_candle, dict):
                close_str = last_candle.get('c')
            else:
                close_str = last_candle[4]  # legacy array format
            return float(close_str)
        # Some endpoints return dict with 'candles' key
        if isinstance(data, dict):
            candles = data.get('candles', [])
            if candles:
                last = candles[-1]
                close_str = last['c'] if isinstance(last, dict) else last[4]
                return float(close_str)
    except Exception:
        # Silently degrade — caller handles None
        pass
    return None


# ─── Main Display ───────────────────────────────────────────────────────────

def print_header(now_utc: datetime.datetime) -> None:
    # Try to fetch BTC perp price (mainnet only — tournament context)
    btc_price = fetch_btc_perp_price(mainnet=True)
    price_str = f' | BTC ${btc_price:,.0f}' if btc_price else ' | BTC —'

    print('═' * 68)
    print(f'  KILL ZONE & PLANETARY HOUR — {now_utc:%Y-%m-%d %H:%M:%S} UTC')
    print(f'  Day: {weekday_name(now_utc.weekday())}  |  Day Ruler: {DAY_RULERS[now_utc.weekday()]}{price_str}')
    print('═' * 68)


def get_kz_hour_sequence(kz_name: str, now_utc: datetime.datetime) -> List[str]:
    """
    Return the ordered list of planetary planets that occur during the given
    kill zone on the relevant date. Used for displaying the full hour sequence
    in the kill-zone table.
    """
    kz_def = KILL_ZONES[kz_name]
    start_utc, end_utc = get_kill_zone_utc_range(kz_name, now_utc.date())
    loc_key = KZ_TO_LOCATION[kz_name]
    # Determine the local date of the KZ start to fetch correct planetary hours
    local_start = start_utc.astimezone(TZ[loc_key])
    local_date = local_start.date()
    # Get all 24 planetary hours for that location/date
    hours = get_planetary_hours_for_date(loc_key, local_date)
    # Filter to those overlapping the KZ's UTC interval
    overlapping = [h for h in hours if h['start_utc'] < end_utc and h['end_utc'] > start_utc]
    overlapping.sort(key=lambda h: h['start_utc'])
    return [h['planet'] for h in overlapping]


def print_kill_zone_table(now_utc: datetime.datetime,
                          active_kz_name: Optional[str]) -> None:
    # Header: Kill Zone table with full planetary hour sequence
    print(f'\n{"Kill Zone":<18} {"UTC Range":<14} {"Planetary Hours"}')
    print('-' * 80)

    for kz_name in KZ_DISPLAY_ORDER:
        kz_def = KILL_ZONES[kz_name]
        label = kz_def['label']
        utc_range = f"{kz_def['start_hour']:02d}:{kz_def['start_min']:02d}-" \
                    f"{kz_def['end_hour']:02d}:{kz_def['end_min']:02d}"

        # Planetary hour sequence during this KZ
        seq_planets = get_kz_hour_sequence(kz_name, now_utc)
        if seq_planets:
            seq_str = ' → '.join(f'{planet_emoji(p)} {p}' for p in seq_planets)
        else:
            seq_str = '—'

        # Mark active KZ with arrow
        prefix = '◀ ' if kz_name == active_kz_name else '  '

        print(f'{prefix}{label:<18} {utc_range:<14} {seq_str}')

# ─── Data & Indicator Helpers ───────────────────────────────────────────────

def fetch_candles(interval: str, count: int, mainnet: bool = True) -> List[Dict]:
    """Fetch the most recent ``count`` candles for BTC perpetual swap from Hyperliquid.

    Args:
        interval: Candlestick interval (e.g. '1m', '4h', '1h').
        count: Number of candles to retrieve.
        mainnet: Use production API if True, else testnet.

    Returns:
        List of candle dictionaries (HL format). Empty list on error.
    """
    api_url = 'https://api.hyperliquid.xyz/info' if mainnet else 'https://api.hyperliquid-testnet.xyz/info'
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    interval_map = {'1m': 60 * 1000, '1h': 60 * 60 * 1000, '4h': 4 * 60 * 60 * 1000}
    interval_ms = interval_map.get(interval, 60 * 1000)

    start_ms = now_ms - count * interval_ms * 2  # buffer to ensure enough candles
    if start_ms < 0:
        start_ms = 0

    payload = {
        'type': 'candleSnapshot',
        'req': {
            'coin': 'BTC',
            'interval': interval,
            'startTime': start_ms,
            'endTime': now_ms,
        }
    }
    try:
        resp = requests.post(api_url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get('candles', [])
    except Exception:
        pass
    return []


def _extract_close(candle) -> float:
    """Extract the close price from a Hyperliquid candle (dict or list)."""
    if isinstance(candle, dict):
        return float(candle['c'])
    else:
        # assume list/tuple; close price at index 4 (or 5 for some formats)
        try:
            return float(candle[4])
        except (IndexError, TypeError):
            return float(candle[5])


def _ema(series: List[float], period: int) -> float:
    """Exponential Moving Average (alpha = 2/(period+1))."""
    if len(series) < period:
        raise ValueError("Series too short for EMA")
    alpha = 2 / (period + 1)
    ema = series[0]
    for price in series[1:]:
        ema = price * alpha + ema * (1 - alpha)
    return ema


def _rsi(series: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI calculation."""
    if len(series) < period + 1:
        return None
    changes = [series[i] - series[i-1] for i in range(1, len(series))]
    gains = [c if c > 0 else 0 for c in changes]
    losses = [-c if c < 0 else 0 for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_indicators_for_candles(
    candles: List[Dict],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (EMA9, EMA21, RSI14) from OHLCV candles."""
    closes = [_extract_close(c) for c in candles]
    ema9 = _ema(closes, 9) if len(closes) >= 9 else None
    ema21 = _ema(closes, 21) if len(closes) >= 21 else None
    rsi14 = _rsi(closes, 14)
    return ema9, ema21, rsi14


def compute_daily_indicators(
    candles: List[Dict],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (EMA20, EMA50, RSI14) for 4-hour candles."""
    closes = [_extract_close(c) for c in candles]
    ema20 = _ema(closes, 20) if len(closes) >= 20 else None
    ema50 = _ema(closes, 50) if len(closes) >= 50 else None
    rsi14 = _rsi(closes, 14)
    return ema20, ema50, rsi14


def print_technical_indicators(
    now_utc: datetime.datetime,
    m1_candles: List[Dict[str, float]],
    d4_candles: List[Dict[str, float]],
) -> None:
    """
    Print EMA and RSI values for 1-minute and 4-hour timeframes.
    Values shown to nearest whole number (price) or 1 decimal (RSI).
    """
    print(f'\n── Technical Indicators ──')

    # 1-minute indicators
    ema9, ema21, rsi14_1m = compute_indicators_for_candles(m1_candles)
    if ema9 is not None and ema21 is not None:
        # Determine EMA trend arrows (relative to each other)
        if ema9 > ema21:
            ema9_arrow = '▲'
            ema21_arrow = '▼'
        elif ema9 < ema21:
            ema9_arrow = '▼'
            ema21_arrow = '▲'
        else:
            ema9_arrow = ema21_arrow = '─'
        ema9_str = f'${ema9:,.0f}'
        ema21_str = f'${ema21:,.0f}'
        rsi_val = rsi14_1m
        rsi_str = f'{rsi_val:.1f}' if rsi_val is not None else '—'
        if rsi_val is not None:
            rsi_arrow = '▲' if rsi_val > 50 else ('▼' if rsi_val < 50 else '─')
            rsi_part = f'RSI(14) {rsi_str} {rsi_arrow}'
        else:
            rsi_part = 'RSI(14) —'
        print(f'  1m: EMA9  {ema9_str} {ema9_arrow}  EMA21 {ema21_str} {ema21_arrow}  {rsi_part}')
    else:
        print(f'  1m: EMA9  —  EMA21 —  RSI(14) —')

    # 4-hour indicators
    ema20, ema50, rsi14_4h = compute_daily_indicators(d4_candles)
    if ema20 is not None and ema50 is not None:
        # Determine EMA trend arrows (relative to each other)
        if ema20 > ema50:
            ema20_arrow = '▲'
            ema50_arrow = '▼'
        elif ema20 < ema50:
            ema20_arrow = '▼'
            ema50_arrow = '▲'
        else:
            ema20_arrow = ema50_arrow = '─'
        ema20_str = f'${ema20:,.0f}'
        ema50_str = f'${ema50:,.0f}'
        rsi_val = rsi14_4h
        rsi_str = f'{rsi_val:.1f}' if rsi_val is not None else '—'
        if rsi_val is not None:
            rsi_arrow = '▲' if rsi_val > 50 else ('▼' if rsi_val < 50 else '─')
            rsi_part = f'RSI(14) {rsi_str} {rsi_arrow}'
        else:
            rsi_part = 'RSI(14) —'
        print(f'  4h: EMA20 {ema20_str} {ema20_arrow}  EMA50 {ema50_str} {ema50_arrow}  {rsi_part}')
    else:
        print(f'  4h: EMA20 —  EMA50 —  RSI(14) —')


def print_current_planetary_hour_detail(
    now_utc: datetime.datetime,
    active_kz_name: str,
    location_key: str
) -> None:
    """Deep-dive on current planetary hour: boundaries, local time, day/night."""
    print(f'\n── Current Planetary Hour — {KILL_ZONES[active_kz_name]["label"]} ──')

    ph = get_planetary_hour_at_time(location_key, now_utc)
    if ph is None:
        print('  ⚠ Could not determine planetary hour (edge-case timestamp?)')
        return

    planet = ph['planet']
    hour_num = ph['hour_number']
    is_day = ph['is_day_hour']
    start_utc = ph['start_utc']
    end_utc = ph['end_utc']
    start_local = ph['start_local']
    end_local = ph['end_local']

    tz = TZ[location_key]
    local_now = now_utc.astimezone(tz)

    # Calculate elapsed / remaining
    elapsed = (local_now - start_local).total_seconds()
    duration = (end_local - start_local).total_seconds()
    elapsed_pct = (elapsed / duration) * 100 if duration > 0 else 0

    hour_type = 'Day' if is_day else 'Night'
    day_dur_h = ((sunset := get_planetary_hours_for_date(location_key, now_utc.date())[11]['end_utc']) -
                 (sunrise := get_planetary_hours_for_date(location_key, now_utc.date())[0]['start_utc'])).total_seconds() / 3600
    night_dur_h = 24 - day_dur_h

    print(f'  Planet:        {planet_emoji(planet)} {planet} {planet_bias(planet).strip()}')
    print(f'  Hour number:   {hour_num} of 24  ({hour_type} hour)')
    print(f'  Local time:    {fmt_time(start_local)} – {fmt_time(end_local)} {tz.zone}')
    print(f'  UTC:           {fmt_time(start_utc)} – {fmt_time(end_utc)}')
    print(f'  Progress:      {elapsed_pct:.1f}%  ({minutes_until(int(elapsed))} elapsed, '
          f'{minutes_until(int(duration - elapsed))} remaining)')
    print(f'  Day length:    {day_dur_h:.2f}h  |  Night length: {night_dur_h:.2f}h')


def print_next_kill_zone(now_utc: datetime.datetime,
                         active_kz_name: Optional[str]) -> None:
    """Show the upcoming kill zone countdown."""
    result = next_kill_zone(now_utc)
    if result is None:
        print('\n── Next Kill Zone ──\n  (no further kill zones found — check logic)')
        return

    next_name, next_start = result
    kz_def = KILL_ZONES[next_name]

    # Compute start in local time for that KZ's city
    location_key = KZ_TO_LOCATION[next_name]
    local_start = next_start.astimezone(TZ[location_key])

    delta = seconds_until(next_start, now_utc)
    delta_str = minutes_until(delta)

    planet = get_planet_for_kz(next_start.weekday(), next_start, next_name) or '?'

    print(f'\n── Next Kill Zone ──')
    print(f'  {kz_def["label"]}: starts in {delta_str}')
    print(f'  UTC start:     {fmt_time(next_start)}')
    print(f'  Local start:   {fmt_time(local_start)} ({TZ[location_key].zone})')
    print(f'  Ruling planet: {planet_emoji(planet)} {planet}{planet_bias(planet)}')


def print_hermetic_insights(planet: str) -> None:
    """Display classical Hermetic correspondences for the current ruling planet."""
    data = HERMETIC_DATA.get(planet)
    if not data:
        return

    print(f'\n⚡ Hermetic Correspondences — {planet} ⚡')
    print(f'  {"Sephira:":<12} {data["sephira"]}')
    print(f'  {"Angel:":<12} {data["angel"]}')
    print(f'  {"Metal:":<12} {data["metal"]}')
    print(f'  {"Color:":<12} {data["color"]}')
    print(f'  {"Day:":<12} {data["day"]}')
    print(f'  {"Virtue:":<12} {data["virtue"]}')
    print(f'  {"Vice:":<12} {data["vice"]}')
    print(f'  {"Tarot:":<12} {data["tarot"]}')
    print(f'  {"Alchemy:":<12} {data["alchemy"]}')



# ─── Hyperliquid Market Microstructure ──────────────────────────────────────

MARKET_API = "https://api.hyperliquid.xyz/info"

@dataclass
class TradeTick:
    """Normalized trade record from HL recentTrades endpoint."""
    side: str           # 'buy' or 'sell'  (B→buy, A→sell)
    price: float        # USD per BTC
    size: float         # BTC contracts
    timestamp_s: float  # epoch seconds
    is_liquidation: bool  # True if hash == all zeros (HL liquidation marker)

def fetch_hl_recent_trades(coin: str = "BTC", limit: int = 500) -> List[TradeTick]:
    """Fetch recent executed trades from Hyperliquid mainnet via REST.
    
    Args:
        coin: Asset symbol (e.g. "BTC")
        limit: Maximum number of trades to return (latest N)
    
    Returns:
        List of TradeTick objects, newest last (chronological order).
        Empty list on any error (network, parse failure, etc.).
    """
    try:
        payload = {"type": "recentTrades", "coin": coin}
        resp = requests.post(MARKET_API, json=payload, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        
        if not isinstance(raw, list):
            return []
        
        trades: List[TradeTick] = []
        # Take last `limit` trades (raw is oldest→newest)
        for t in raw[-limit:]:
            side_raw = t.get("side", "B")
            side = "buy" if side_raw == "B" else "sell"
            price_str = t.get("px")
            size_str = t.get("sz")
            time_ms = t.get("time", int(_time.time() * 1000))
            hash_str = t.get("hash", "")
            
            if price_str is None or size_str is None:
                continue
            
            # Liquidation detection: HL uses all-zero hash for liquidation prints
            is_liq = (hash_str == "0x0000000000000000000000000000000000000000000000000000000000000000")
            
            try:
                price = float(price_str)
                size = float(size_str)
                ts = time_ms / 1000.0
                trades.append(TradeTick(
                    side=side,
                    price=price,
                    size=size,
                    timestamp_s=ts,
                    is_liquidation=is_liq
                ))
            except (ValueError, TypeError):
                continue
        return trades
    except Exception:
        # Silent degradation — caller handles empty list
        return []



def query_hl_trade_buffer(coin: str = "BTC", minutes: int = 60) -> List[TradeTick]:
    """Query the HL trade buffer SQLite DB for recent trades.
    
    Requires the hl_trade_buffer daemon to be running.
    Returns empty list if DB missing or any error.
    """
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "hl_trades.db")
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        return []
    
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            since = _time.time() - minutes * 60
            rows = conn.execute(
                "SELECT timestamp, price, size, side, is_liquidation FROM trades "
                "WHERE timestamp >= ? AND coin = ? ORDER BY timestamp ASC",
                (since, coin)
            ).fetchall()
            trades: List[TradeTick] = []
            for r in rows:
                trades.append(TradeTick(
                    side="buy" if r["side"] == "B" else "sell",
                    price=float(r["price"]),
                    size=float(r["size"]),
                    timestamp_s=float(r["timestamp"]),
                    is_liquidation=bool(r["is_liquidation"])
                ))
            return trades
    except Exception:
        return []

def fetch_hl_asset_context(coin: str = "BTC") -> Dict[str, Optional[float]]:
    """Fetch perp asset context: OI, funding, mark, mid, oracle, premium.

    Uses coin parameter to find the asset in the universe; no hardcoded index.
    All numeric values are strings; converted to float.
    """
    try:
        payload = {"type": "metaAndAssetCtxs"}
        resp = requests.post(MARKET_API, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            return {}
        universe = data[0]  # list of asset info dicts with 'name' field
        asset_ctxs = data[1]
        if not isinstance(asset_ctxs, list) or len(asset_ctxs) == 0:
            return {}
        # Find coin index in universe
        coin_idx = None
        for i, asset in enumerate(universe):
            if isinstance(asset, dict) and asset.get('name') == coin:
                coin_idx = i
                break
        if coin_idx is None:
            return {}
        if coin_idx >= len(asset_ctxs):
            return {}
        ctx = asset_ctxs[coin_idx]
        if not isinstance(ctx, dict):
            return {}
        result: Dict[str, Optional[float]] = {}
        for hl_key, dest_key in [
            ("openInterest", "oi"),
            ("funding", "funding"),
            ("markPx", "mark_px"),
            ("midPx", "mid_px"),
            ("oraclePx", "oracle_px"),
            ("prevDayPx", "prev_day_px"),
            ("premium", "premium"),
        ]:
            val = ctx.get(hl_key)
            if val is not None:
                try:
                    result[dest_key] = float(val)
                except (ValueError, TypeError):
                    pass
        return result
    except Exception:
        return {}

def fetch_hl_current_price(coin: str = "BTC") -> Optional[float]:
    """Get current perp mid price via allMids or asset context."""
    try:
        resp = requests.post(MARKET_API, json={"type": "allMids"}, timeout=8)
        resp.raise_for_status()
        mids = resp.json()
        if isinstance(mids, dict):
            coin_mid = mids.get(coin)
            if coin_mid is not None:
                return float(coin_mid)
    except Exception:
        pass
    try:
        ctx = fetch_hl_asset_context(coin)
        if ctx:
            if "mid_px" in ctx and ctx["mid_px"]:
                return ctx["mid_px"]
            if "oracle_px" in ctx and ctx["oracle_px"]:
                return ctx["oracle_px"]
    except Exception:
        pass
    return None


def compute_trap_pressure(trades: List[TradeTick], current_price: Optional[float]) -> Dict[str, object]:
    """Count and classify trapped longs vs trapped shorts."""
    if current_price is None:
        return {
            "trapped_longs": 0, "trapped_shorts": 0,
            "avg_entry_trapped_long": None, "avg_entry_trapped_short": None,
            "total_longs": 0, "total_shorts": 0,
            "net_flow_btc": 0.0,
        }
    
    trapped_long_prices: List[float] = []
    trapped_short_prices: List[float] = []
    total_longs = total_shorts = 0
    net_flow = 0.0
    
    for t in trades:
        if t.side == "buy":
            total_longs += 1
            net_flow += t.size
            if t.price > current_price:
                trapped_long_prices.append(t.price)
        elif t.side == "sell":
            total_shorts += 1
            net_flow -= t.size
            if t.price < current_price:
                trapped_short_prices.append(t.price)
    
    avg_tl = (sum(trapped_long_prices) / len(trapped_long_prices)) if trapped_long_prices else None
    avg_ts = (sum(trapped_short_prices) / len(trapped_short_prices)) if trapped_short_prices else None
    
    return {
        "trapped_longs": len(trapped_long_prices),
        "trapped_shorts": len(trapped_short_prices),
        "avg_entry_trapped_long": avg_tl,
        "avg_entry_trapped_short": avg_ts,
        "total_longs": total_longs,
        "total_shorts": total_shorts,
        "net_flow_btc": net_flow,
    }


def compute_cumulative_delta_windows(
    trades: List[TradeTick],
    windows_min: Tuple[int, ...] = (1, 5, 15, 60)
) -> Dict[str, float]:
    """Compute net long pressure (cumulative delta) over multiple time windows."""
    if not trades:
        return {f"{w}m": 0.0 for w in windows_min}
    
    now_ts = _time.time()
    results: Dict[str, float] = {}
    sorted_trades = sorted(trades, key=lambda t: t.timestamp_s)
    
    for w in windows_min:
        cutoff = now_ts - (w * 60)
        cum = 0.0
        for t in sorted_trades:
            if t.timestamp_s >= cutoff:
                cum += t.size if t.side == "buy" else -t.size
        results[f"{w}m"] = cum
    
    return results


def _pct(numerator: float, denominator: float) -> str:
    """Format ratio as percentage string."""
    if denominator == 0:
        return "—"
    pct = (numerator / denominator) * 100
    return f"{pct:.0f}%"




def analyze_liquidations(trades: List[TradeTick], lookback_minutes: int = 60) -> Dict[str, object]:
    """Analyze liquidations within recent trades."""
    cutoff = _time.time() - (lookback_minutes * 60)
    window_trades = [t for t in trades if t.timestamp_s >= cutoff]
    liqs = [t for t in window_trades if t.is_liquidation]
    
    total = len(liqs)
    long_liqs = sum(1 for t in liqs if t.side == "sell")
    short_liqs = sum(1 for t in liqs if t.side == "buy")
    
    total_notional = sum(t.price * t.size for t in liqs)
    long_notional = sum(t.price * t.size for t in liqs if t.side == "sell")
    short_notional = sum(t.price * t.size for t in liqs if t.side == "buy")
    
    largest_liq_usd = 0.0
    largest_liq_side = None
    if liqs:
        largest = max(liqs, key=lambda t: t.price * t.size)
        largest_liq_usd = largest.price * largest.size
        largest_liq_side = "LONG" if largest.side == "sell" else "SHORT"
    
    liqs_per_min = total / lookback_minutes if lookback_minutes > 0 else 0.0
    
    return {
        "total_count": total,
        "long_liquidations": long_liqs,
        "short_liquidations": short_liqs,
        "total_notional_usd": total_notional,
        "long_notional_usd": long_notional,
        "short_notional_usd": short_notional,
        "largest_liq_usd": largest_liq_usd,
        "largest_liq_side": largest_liq_side,
        "liqs_per_minute": liqs_per_min,
    }


def print_liquidations_section(trades: List[TradeTick]) -> None:
    """Print liquidation radar section."""
    stats = analyze_liquidations(trades, lookback_minutes=60)
    
    if stats["total_count"] == 0:
        print("  Recent Liquidations:  —  (none in last hour)")
        return
    
    total = stats["total_count"]
    long_liqs = stats["long_liquidations"]
    short_liqs = stats["short_liquidations"]
    total_notional = stats["total_notional_usd"]
    largest_usd = stats["largest_liq_usd"]
    largest_side = stats["largest_liq_side"] or "—"
    rate = stats["liqs_per_minute"]
    
    if rate >= 2.0:
        activity = "🔥 HIGH"
    elif rate >= 1.0:
        activity = "⚠️  ELEVATED"
    else:
        activity = "✓ normal"
    
    if long_liqs > short_liqs * 1.5:
        dominant = "🔴 LONG LIQs dominate (cascade ↓)"
    elif short_liqs > long_liqs * 1.5:
        dominant = "🟢 SHORT LIQs dominate (squeeze ↑)"
    else:
        dominant = "⚪ MIXED"
    
    print(f"  Recent Liquidations (60min):  {total}  (${total_notional:,.0f})  [{activity}]")
    print(f"     Longs rekt (A-side):  {long_liqs:>3d}  (${stats['long_notional_usd']:,.0f})")
    print(f"     Shorts rekt (B-side): {short_liqs:>3d}  (${stats['short_notional_usd']:,.0f})")
    if largest_usd > 0:
        print(f"     Largest: ${largest_usd:,.0f}  ({largest_side})  {dominant}")

def print_market_microstructure(coin: str = "BTC") -> None:
    """Fetch HL microstructure data and print radar section. Degrades gracefully."""
    print(f"\n── Market Microstructure (Hyperliquid) ──")
    
    # Try trade buffer first; fall back to REST if daemon not running
    trades = query_hl_trade_buffer(coin, minutes=60)
    if not trades:
        print("[WARN] Trade buffer offline — using REST snapshot (max 10 trades)")
        trades = fetch_hl_recent_trades(coin, limit=500)
    else:
        print(f"[INFO] Trade buffer online — {len(trades)} trades loaded")
    ctx = fetch_hl_asset_context(coin)
    current_price = fetch_hl_current_price(coin)
    
    if current_price is None:
        print("  Price unavailable — microstructure skipped")
        return
    
    oi = ctx.get("oi")
    oi_str = f"{oi:,.0f} {coin}" if oi is not None else "—"
    funding = ctx.get("funding")
    if funding is not None:
        funding_pct = funding * 100
        funding_annual = funding * 24 * 365 * 100
        funding_str = f"{funding_pct:+.6f}%  (≈ {funding_annual:+.1f}% annual)"
    else:
        funding_str = "—"
    
    print(f"  {coin} Perpetual Swap")
    print(f"  Current Price:   ${current_price:,.0f}  (mid)")
    print(f"  Open Interest:   {oi_str}")
    print(f"  Funding Rate:    {funding_str}")
    
    if trades:
        cumdeltas = compute_cumulative_delta_windows(trades, windows_min=(1, 5, 15, 60))
        print("  Cumulative Delta:")
        for label in ["1m", "5m", "15m", "60m"]:
            val = cumdeltas.get(label, 0.0)
            sign = "+" if val >= 0 else ""
            window_min = int(label.rstrip("m"))
            cutoff = _time.time() - (window_min * 60)
            window_trades = [t for t in trades if t.timestamp_s >= cutoff]
            total_vol = sum(t.size for t in window_trades)
            long_vol = sum(t.size for t in window_trades if t.side == "buy")
            pct_str = _pct(long_vol, total_vol) if total_vol > 0 else "—"
            print(f"     {label:>3s}: {sign}{val:>8.1f} BTC  ({pct_str} longs)")
        
        trap = compute_trap_pressure(trades, current_price)
        tl, ts = trap["trapped_longs"], trap["trapped_shorts"]
        avg_tl = trap["avg_entry_trapped_long"]
        avg_ts = trap["avg_entry_trapped_short"]
        
        if tl > ts:
            pressure_emoji = "🔴"
            pressure_text = "longs trapped → liquidation risk ↓"
        elif ts > tl:
            pressure_emoji = "🟢"
            pressure_text = "shorts trapped → squeeze fuel ↑"
        else:
            pressure_emoji = "⚪"
            pressure_text = "balanced trap pressure"
        
        print(f"  Net Trapped:")
        print(f"     Longs above:  {tl:>3d}  (avg entry: ${avg_tl:,.0f})" if avg_tl else f"     Longs above:  {tl:>3d}")
        print(f"     Shorts below: {ts:>3d}  (avg entry: ${avg_ts:,.0f})" if avg_ts else f"     Shorts below: {ts:>3d}")
        print(f"     Pressure:     {pressure_emoji}  {pressure_text}")
        
        cutoff_5m = _time.time() - (5 * 60)
        vol_5m = [t for t in trades if t.timestamp_s >= cutoff_5m]
        buy_vol_5m = sum(t.size for t in vol_5m if t.side == "buy")
        sell_vol_5m = sum(t.size for t in vol_5m if t.side == "sell")
        total_5m = buy_vol_5m + sell_vol_5m
        buy_pct = (buy_vol_5m / total_5m * 100) if total_5m > 0 else 0.0
        sell_pct = (sell_vol_5m / total_5m * 100) if total_5m > 0 else 0.0
        net_5m = trap["net_flow_btc"]
        flow_arrow = "▲" if net_5m >= 0 else "▼"
        def sign_str(x: float) -> str:
            if x > 0: return f"+{x:.1f} BTC"
            elif x < 0: return f"{x:.1f} BTC"
            else: return "0.0 BTC"
        print(f"  Last 5m Flow:   BUY {buy_pct:>4.0f}%  |  SELL {sell_pct:>4.0f}%  {flow_arrow}  (net {sign_str(net_5m)})")
        print_liquidations_section(trades)
    else:
        print("  Cumulative Delta:  —  (no recent trades)")
        print("  Net Trapped:      —")
        print("  Last 5m Flow:     —")
        print_liquidations_section(trades)


def print_footer(now_utc: datetime.datetime) -> None:
    print(f'\n  Updated: {now_utc:%Y-%m-%d %H:%M:%S} UTC')
    print('═' * 68)


# ─── Entrypoint ─────────────────────────────────────────────────────────────

def main() -> int:
    now_utc = datetime.datetime.now(pytz.UTC)

    # 1. Determine active kill zone and current planetary hour
    active_kz = get_active_kill_zone(now_utc)
    location_key = None
    ph = None
    if active_kz and active_kz in KZ_TO_LOCATION:
        location_key = KZ_TO_LOCATION[active_kz]
        ph = get_planetary_hour_at_time(location_key, now_utc)

    # 2. Header
    print_header(now_utc)

    # 3. Current Planetary Hour Detail (before kill zone table)
    if active_kz and location_key and ph:
        print_current_planetary_hour_detail(now_utc, active_kz, location_key)
        if ph:
            print_hermetic_insights(ph['planet'])
    else:
        print('\n── Current Planetary Hour ──')
        print('  ⚠ No active kill zone — the 6 KZs should cover all 24 hours.')
        print('  This indicates a gap in KZ definitions or DST issue.')

    # 4. Kill zone table (with planetary hour sequences)
    print_kill_zone_table(now_utc, active_kz)

    # 5. Fetch technical indicator data (non-blocking, best-effort)
    m1_candles = fetch_candles('1m', count=100, mainnet=True)
    d4_candles = fetch_candles('4h', count=80, mainnet=True)

    # 6. Technical indicators display
    print_technical_indicators(now_utc, m1_candles, d4_candles)

    # 6a. Market microstructure radar
    print_market_microstructure(args.asset)

    # 7. Lunar phase context
    print_lunar_phases(now_utc)

    # 8. Next kill zone
    print_next_kill_zone(now_utc, active_kz)

    # 9. Footer
    print_footer(now_utc)

    return 0


if __name__ == '__main__':
    sys.exit(main())
