# ETF Momentum Rotator Bot

Scans 154 ETFs across US, international, emerging markets, commodities, bonds, leveraged, thematic, and dividend categories. Ranks by momentum, buys top performers in confirmed uptrends, sells on trend breaks, rotates to safety in bear markets.

## Setup

1. Push to GitHub
2. Create Render Web Service from repo
3. Set environment variables in Render dashboard
4. Bot runs automatically: Full scan Monday, Pulse check Friday, Status daily

## Commands (local testing)

```bash
python etf_rotator.py scan      # Run full scan now
python etf_rotator.py pulse     # Run pulse check
python etf_rotator.py status    # Quick portfolio status
python etf_rotator.py balance   # Show account balance
python etf_rotator.py positions # Show current positions
python etf_rotator.py test SPY  # Test scoring on one ticker
```

## Environment Variables

- `TRADIER_ACCOUNT_ID` - Your Tradier account ID
- `TRADIER_API_KEY` - Your Tradier API key
- `TELEGRAM_TOKEN` - Telegram bot token (optional)
- `TELEGRAM_CHAT_ID` - Telegram chat ID (optional)
