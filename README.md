# PBS Vibes Trading System

Planetary-Based Strategy (PBS) discretionary trading system for Hyperliquid.

## Architecture

```
pbs_vibe_check.py  →  stdout report  →  Hermes cronjob  →  vibes decision
     (telescope)                                           (astrologer)
```

One script calculates everything. Hermes reads it and makes the call.
No mechanical scoring. No conviction numbers. Pure vibes.

## What It Does

- Per-KZ planetary hours (Chaldean order, city-specific sunrise/sunset via `astral`)
- Lunar phase (pure math, synodic month)
- Technical indicators (EMA 9/21 on 1m, EMA 20/50 on 4h, RSI)
- Market microstructure (cumulative delta, trapped traders, liquidations, OI, funding)
- Portfolio state from Hyperliquid (source of truth — no manual PnL)
- Hermetic correspondences for the active planetary hour

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # add your HL wallet credentials
python3 pbs_vibe_check.py
python3 pbs_vibe_check.py --coins BTC ETH SOL
```

## Kill Zones (UTC)

| Zone           | Window       | Location    |
|----------------|-------------|-------------|
| Singapore      | 23:00-03:00 | Singapore   |
| Dubai          | 03:00-07:00 | Dubai       |
| London         | 07:00-12:00 | London      |
| NY AM          | 12:00-15:00 | New York    |
| London Close   | 15:00-17:00 | London      |
| NY PM          | 17:00-23:00 | New York    |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HL_MAIN_WALLET` | Hyperliquid wallet address |
| `HL_SECRET_KEY` | Wallet private key (for execution) |
| `HL_WALLET_API` | API wallet address |
| `USE_TESTNET` | `true` for testnet, `false` for mainnet |

## Files

- `pbs_vibe_check.py` — The script (telescope)
- `engine/planetary_hours.py` — Chaldean planetary hour engine
- `scripts/prompt_template.txt` — Hermes cronjob prompt template
- `.env` — Credentials (not in git)
