# HK Tech Preopen Bot

This bot generates a Telegram-friendly pre-open trading report for Hang Seng Tech Index futures.

## Schedule

- Intended send time: `10:20` in `Australia/Melbourne`
- GitHub Actions runs at two UTC times to handle Melbourne daylight saving:
  - `23:20 UTC`
  - `00:20 UTC`
- The script uses a Melbourne-local schedule guard and only sends when the local time is near `10:20` on weekdays.

## Required GitHub secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## What the bot does

- Uses the previous US close to assess HK tech futures bias
- Confirms Asian risk appetite with Japan, Korea, and Australia open
- Tracks Nasdaq, QQQ, KWEB, key China ADRs, US 10Y yield proxy, DXY, USD/CNH, USD/JPY, and AUD/USD
- Produces three Telegram-friendly messages:
  - core conclusion + trading plan
  - data snapshot + transmission view
  - risks + watchlist
- Writes local markdown and html copies
- Sends a Telegram failure alert if generation fails

## Notes

- The bot avoids inventing data.
- If key inputs are missing, it sends a `数据不足 / 不适合盘前交易` version instead of forcing a direction.
- Free public data does not reliably provide Hang Seng Tech night futures; the bot uses ADR / ETF / Asia open proxies and explicitly marks unavailable items.

## Manual local run

```bash
cp .env.example .env
# fill in your Telegram values
python3 send_report.py --env-file .env --force
```

