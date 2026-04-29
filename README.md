# PBS Vibes Trader
Hyperliquid testnet discretionary trading system implementing **PBS (Planetary-Based Strategy)** with vibes-driven execution, Chaldean planetary hour alignment, and real-time Hyperliquid market microstructure analysis.

> PBS Mandate: No leverage, 1.0x max equity, Singapore kill-zone (KZ) alignment, VWAP/TWAP-centered entries, higher trade count preferred.

## ✨ Core Features
- **Planetary Intelligence**: Accurate unequal planetary hours from city-specific sunrise/sunset, moon phase tracking, kill-zone confluence scoring
- **Hyperliquid Integration**: WebSocket trade buffer for top 5 volume assets (BTC/ETH/SOL/MATIC/DOGE), perp/spot portfolio balance tracking, position monitoring
- **Bulletproof Vibes Engine**: Rule-based trade signals with zero AI hallucination, exact script output pasting to Telegram
- **Cron Automation**: 5-minute interval unified runner via Hermes Agent cronjob tool (no system crontab)
- **Event-Sourced State**: Persistent `hermes_vibes_state.json` for position tracking

## 📋 Prerequisites
- Python 3.10+
- Hyperliquid testnet API key + secret (`HL_WALLET_API`, `HL_SECRET_KEY`)
- Telegram bot token (`TELEGRAM_BOT_TOKEN`)
- Bun (optional, for gbrain knowledge base integration)
- Hermes Agent (for cron job management)

## 🚀 Quick Setup
1. Clone repo:
   ```bash
   git clone https://github.com/periptheo-pbs/pbs-vibes.git
   cd pbs-vibes
   ```
2. Create `.env` (replace `[REDACTED]` with real credentials):
   ```env
   # Hyperliquid
   HL_WALLET_API=0xdbfd2e620d0b5c1a28766565627aef02653b1180
   HL_SECRET_KEY=[REDACTED]
   HL_MAIN_WALLET=0x2601718c5F832f5F4540f7D035b315eE718f5d48
   # Telegram
   TELEGRAM_BOT_TOKEN=[REDACTED]
   # GBrain (optional)
   OPENAI_API_KEY=[REDACTED]
   ```
3. Install dependencies:
   ```bash
   pip install hyperliquid-python-sdk pandas requests python-dotenv
   ```

## 💻 Usage
### Run Core Scripts
| Script | Purpose |
|--------|---------|
| `scripts/kz_calculator.py` | Outputs BTC price, planetary hours, kill zones, microstructure, portfolio balance |
| `scripts/hl_trade_buffer.py` | Background WebSocket daemon collecting Hyperliquid trades to `data/hl_trades.db` |
| `scripts/vibes_unified_runner.py` | Cron entry point: runs kz_calculator, formats Telegram summary with no hallucination |

### Start Trade Buffer Daemon
```bash
python scripts/hl_trade_buffer.py &
# Check logs: tail -f logs/trade_buffer_live.log
```

### Schedule Cron Job (Hermes Agent)
```bash
# From Hermes Agent CLI/Telegram:
cronjob create --name "Vibes Trader Unified" --schedule "*/5 * * * *" --prompt "Run python scripts/vibes_unified_runner.py and paste exact output to Telegram"
```

## 📂 Project Structure
```
scripts/
  kz_calculator.py        # Core analysis: planetary + market + portfolio
  hl_trade_buffer.py      # Hyperliquid WebSocket trade buffer
  vibes_unified_runner.py # Cron wrapper (no AI hallucination)
  vibes_execute.py        # Trade execution logic (unstaged)
live/
  hermes_vibes_state.json # Position state persistence
data/
  hl_trades.db            # SQLite trade buffer database
logs/
  trade_buffer_live.log   # Trade buffer daemon logs
```

## ⚠️ Known Issues
See [GitHub Issues](https://github.com/periptheo-pbs/pbs-vibes/issues) for active bugs:
1. Testnet portfolio balance API returns $0.00 despite confirmed $974.49 balance
2. HL trade log PnL calculation broken (only Saturnalia correct)
3. kz_calculator.py fails to fetch testnet wallet balances across all addresses

## License
MIT
