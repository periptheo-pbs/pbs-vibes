"""
Planetary Hours Engine
======================
Calculates planetary hours based on sunrise/sunset for any location.
The Chaldean order: Saturn, Jupiter, Mars, Sun, Venus, Mercury, Moon
Each day starts at sunrise with its ruling planet.

Day rulers:
  Sunday    = Sun
  Monday    = Moon
  Tuesday   = Mars
  Wednesday = Mercury
  Thursday  = Jupiter
  Friday    = Venus
  Saturday  = Saturn

Planetary hours are NOT equal 60-minute hours. They divide the daylight
period into 12 equal parts (day hours) and the nighttime into 12 equal
parts (night hours). So actual duration varies by season and latitude.

Kill Zones (all in UTC, no DST adjustments needed):
  Singapore Zone:   23:00 - 03:00 UTC
  Dubai Zone:      03:00 - 07:00 UTC
  London Zone:      07:00 - 12:00 UTC
  NY AM Zone:       12:00 - 15:00 UTC
  London Close:     15:00 - 17:00 UTC
  NY PM Zone:       17:00 - 23:00 UTC
"""

import datetime
import pytz
from astral import LocationInfo
from astral.sun import sun
from typing import List, Tuple, Optional, Dict

# Chaldean order (fastest apparent motion = closest sphere to earth)
CHALDEAN_ORDER = ['Saturn', 'Jupiter', 'Mars', 'Sun', 'Venus', 'Mercury', 'Moon']

# Day of week -> ruling planet (ruler of the first hour at sunrise)
DAY_RULERS = {
    0: 'Moon',      # Monday
    1: 'Mars',      # Tuesday
    2: 'Mercury',   # Wednesday
    3: 'Jupiter',   # Thursday
    4: 'Venus',     # Friday
    5: 'Saturn',    # Saturday
    6: 'Sun',       # Sunday
}

# Key trading locations (used for sunrise/sunset calculations)
LOCATIONS = {
    'singapore': LocationInfo("Singapore", "Singapore", "Asia/Singapore", 1.3521, 103.8198),
    'dubai': LocationInfo("Dubai", "UAE", "Asia/Dubai", 25.2048, 55.2708),
    'london': LocationInfo("London", "England", "Europe/London", 51.5074, -0.1278),
    'new_york': LocationInfo("New York", "USA", "America/New_York", 40.7128, -74.0060),
}

# 24/7 trading zones in UTC. These are not kill zones for forced exits anymore;
# they split the full day so each timestamp uses the correct local planetary-hour calculation.
KILL_ZONES = {
    'singapore': {
        'start_hour': 23, 'start_min': 0,
        'end_hour': 3, 'end_min': 0,
        'tz': 'UTC',
        'label': 'Singapore Zone',
    },
    'dubai': {
        'start_hour': 3, 'start_min': 0,
        'end_hour': 7, 'end_min': 0,
        'tz': 'UTC',
        'label': 'Dubai Zone',
    },
    'london': {
        'start_hour': 7, 'start_min': 0,
        'end_hour': 12, 'end_min': 0,
        'tz': 'UTC',
        'label': 'London Zone',
    },
    'new_york_am': {
        'start_hour': 12, 'start_min': 0,
        'end_hour': 15, 'end_min': 0,
        'tz': 'UTC',
        'label': 'NY AM Zone',
    },
    'london_close': {
        'start_hour': 15, 'start_min': 0,
        'end_hour': 17, 'end_min': 0,
        'tz': 'UTC',
        'label': 'London Close Zone',
    },
    'new_york_pm': {
        'start_hour': 17, 'start_min': 0,
        'end_hour': 23, 'end_min': 0,
        'tz': 'UTC',
        'label': 'NY PM Zone',
    },
}

# Map zone names to location keys for planetary hour calculation
KZ_TO_LOCATION = {
    'singapore': 'singapore',
    'dubai': 'dubai',
    'london': 'london',
    'new_york_am': 'new_york',
    'london_close': 'london',
    'new_york_pm': 'new_york',
}


def get_planetary_hour_sequence(day_of_week: int) -> List[str]:
    """
    Get the 24-hour planetary sequence for a given day of week.
    day_of_week: 0=Monday, 6=Sunday (Python convention)
    Returns list of 24 planet names [hour1_sunrise, hour2, ... hour12_day, hour13_sunset, ... hour24_night]
    """
    ruler = DAY_RULERS[day_of_week]
    start_idx = CHALDEAN_ORDER.index(ruler)
    
    sequence = []
    for i in range(24):
        planet = CHALDEAN_ORDER[(start_idx + i) % 7]
        sequence.append(planet)
    return sequence


def get_sunrise_sunset(location_key: str, date: datetime.date) -> Tuple[datetime.datetime, datetime.datetime]:
    """Get sunrise and sunset for a location on a given date."""
    loc = LOCATIONS[location_key]
    s = sun(loc.observer, date=date, tzinfo=pytz.timezone(loc.timezone))
    return s['sunrise'], s['sunset']


def get_planetary_hours_for_date(location_key: str, date: datetime.date) -> List[Dict]:
    """
    Calculate all 24 planetary hours for a specific date and location.
    Returns list of dicts with: planet, start_utc, end_utc, is_day_hour, hour_number
    
    Day hours: sunrise to sunset divided into 12 equal parts
    Night hours: sunset to next sunrise divided into 12 equal parts
    
    The first hour at sunrise is ruled by the day's ruling planet.
    Subsequent hours follow the Chaldean order: Saturn -> Jupiter -> Mars -> Sun -> Venus -> Mercury -> Moon
    repeating continuously through day and night.
    """
    loc = LOCATIONS[location_key]
    tz = pytz.timezone(loc.timezone)
    
    # Get sunrise/sunset for today and sunrise for tomorrow
    s_today = sun(loc.observer, date=date, tzinfo=tz)
    next_date = date + datetime.timedelta(days=1)
    s_tomorrow = sun(loc.observer, date=next_date, tzinfo=tz)
    
    sunrise = s_today['sunrise']
    sunset = s_today['sunset']
    next_sunrise = s_tomorrow['sunrise']
    
    # Day hour duration (sunrise to sunset / 12)
    day_duration = (sunset - sunrise).total_seconds()
    day_hour_secs = day_duration / 12.0
    
    # Night hour duration (sunset to next sunrise / 12)
    night_duration = (next_sunrise - sunset).total_seconds()
    night_hour_secs = night_duration / 12.0
    
    # Get the planet sequence for this day
    day_of_week = date.weekday()
    sequence = get_planetary_hour_sequence(day_of_week)
    
    hours = []
    for i in range(24):
        if i < 12:
            # Day hours (sunrise to sunset)
            start = sunrise + datetime.timedelta(seconds=day_hour_secs * i)
            end = sunrise + datetime.timedelta(seconds=day_hour_secs * (i + 1))
            is_day = True
        else:
            # Night hours (sunset to next sunrise)
            ni = i - 12
            start = sunset + datetime.timedelta(seconds=night_hour_secs * ni)
            end = sunset + datetime.timedelta(seconds=night_hour_secs * (ni + 1))
            is_day = False
        
        hours.append({
            'planet': sequence[i],
            'start_local': start,
            'end_local': end,
            'start_utc': start.astimezone(pytz.UTC),
            'end_utc': end.astimezone(pytz.UTC),
            'is_day_hour': is_day,
            'hour_number': i + 1,
        })
    
    return hours


def get_planetary_hour_at_time(location_key: str, dt_utc: datetime.datetime) -> Optional[Dict]:
    """
    Given a UTC datetime, find which planetary hour is active at a specific location.
    Returns dict with planet info or None if can't determine.
    """
    loc = LOCATIONS[location_key]
    tz = pytz.timezone(loc.timezone)
    local_dt = dt_utc.astimezone(tz)
    local_date = local_dt.date()
    
    # Try today's hours first
    hours = get_planetary_hours_for_date(location_key, local_date)
    for h in hours:
        if h['start_utc'] <= dt_utc < h['end_utc']:
            return h
    
    # If not found, try previous day's night hours
    prev_date = local_date - datetime.timedelta(days=1)
    hours = get_planetary_hours_for_date(location_key, prev_date)
    for h in hours:
        if h['start_utc'] <= dt_utc < h['end_utc']:
            return h
    
    return None


def is_in_kill_zone(kz_name: str, dt_utc: datetime.datetime) -> bool:
    """Check if a UTC timestamp falls within the specified kill zone."""
    kz = KILL_ZONES[kz_name]
    
    # Kill zones are defined in UTC directly
    utc_hour = dt_utc.hour
    utc_min = dt_utc.minute
    local_minutes = utc_hour * 60 + utc_min
    start_minutes = kz['start_hour'] * 60 + kz['start_min']
    end_minutes = kz['end_hour'] * 60 + kz['end_min']
    
    if start_minutes <= end_minutes:
        return start_minutes <= local_minutes < end_minutes
    else:
        # Crosses midnight (e.g. 23:00-02:00)
        return local_minutes >= start_minutes or local_minutes < end_minutes


def get_active_kill_zone(dt_utc: datetime.datetime) -> Optional[str]:
    """Return the name of the active kill zone at the given UTC time, or None."""
    for kz_name in KILL_ZONES:
        if is_in_kill_zone(kz_name, dt_utc):
            return kz_name
    return None


def get_kill_zone_utc_range(kz_name: str, date: datetime.date) -> Tuple[datetime.datetime, datetime.datetime]:
    """Get the UTC start and end times for a kill zone on a specific date."""
    kz = KILL_ZONES[kz_name]
    
    start_utc = pytz.UTC.localize(datetime.datetime(date.year, date.month, date.day, kz['start_hour'], kz['start_min']))
    end_utc = pytz.UTC.localize(datetime.datetime(date.year, date.month, date.day, kz['end_hour'], kz['end_min']))
    
    if end_utc <= start_utc:
        end_utc += datetime.timedelta(days=1)
    
    return start_utc, end_utc


def get_planet_for_kz(weekday: int, dt_utc: datetime.datetime, kz_name: str) -> Optional[str]:
    """
    Get the ruling planet for a given UTC time within a specific kill zone.
    Uses proper sunrise/sunset-based planetary hour calculation for the
    location associated with that kill zone.
    """
    location_key = KZ_TO_LOCATION.get(kz_name)
    if location_key is None:
        return None
    
    ph = get_planetary_hour_at_time(location_key, dt_utc)
    if ph is not None:
        return ph['planet']
    
    return None


# Quick test
if __name__ == '__main__':
    today = datetime.date.today()
    print(f"Date: {today} ({today.strftime('%A')})")
    print(f"Day ruler: {DAY_RULERS[today.weekday()]}")
    print()
    
    for loc_key in ['singapore', 'london', 'new_york']:
        print(f"\n=== {loc_key.upper()} ===")
        hours = get_planetary_hours_for_date(loc_key, today)
        for h in hours[:6]:
            print(f"  Hour {h['hour_number']:2d}: {h['planet']:8s} | {h['start_utc'].strftime('%H:%M')} - {h['end_utc'].strftime('%H:%M')} UTC | {'Day' if h['is_day_hour'] else 'Night'}")
        print(f"  ...")
        for h in hours[12:14]:
            print(f"  Hour {h['hour_number']:2d}: {h['planet']:8s} | {h['start_utc'].strftime('%H:%M')} - {h['end_utc'].strftime('%H:%M')} UTC | {'Day' if h['is_day_hour'] else 'Night'}")
    
    print(f"\n=== KILL ZONES (UTC) ===")
    now_utc = datetime.datetime.now(pytz.UTC)
    for kz_name, kz in KILL_ZONES.items():
        kz_start, kz_end = get_kill_zone_utc_range(kz_name, today)
        active = is_in_kill_zone(kz_name, now_utc)
        print(f"  {kz['label']}: {kz_start.strftime('%H:%M')} - {kz_end.strftime('%H:%M')} UTC {'[ACTIVE NOW]' if active else ''}")
    
    # Test planetary hour for current time in each KZ
    print(f"\n=== CURRENT PLANETARY HOURS ({now_utc.strftime('%Y-%m-%d %H:%M')} UTC) ===")
    for kz_name in KILL_ZONES:
        if is_in_kill_zone(kz_name, now_utc):
            planet = get_planet_for_kz(now_utc.weekday(), now_utc, kz_name)
            print(f"  {KILL_ZONES[kz_name]['label']}: {planet}")
