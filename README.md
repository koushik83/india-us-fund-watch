# India US Fund Watch

Tracks Indian mutual funds that invest in US equities (Nasdaq 100, S&P 500,
FANG+, US-focused FoFs) and sends a Telegram alert when any of them reopens
for lumpsum or SIP. Most of them are shut because the industry's overseas
investment limit is used up, and when one reopens the headroom fills fast.

## What it does

**One Telegram message a day, max**, sent on the first run after 09:00 IST:

- 🇺🇸 Summary: lumpsum open / SIP-only / closed, plus anything that changed since yesterday
- 🚨 A banner on top if any fund opened
- ⚠️ "watcher is broken" instead, if the data source fails (✅ noted on the first good day after)

The workflow fires hourly only as retries, because GitHub's cron silently skips runs.
Run it manually from the Actions tab to force a check + message now.

- **Source:** Kuvera's public fund API (`lump_available`, `sip_available`, `lump_max`).
  These are Kuvera's flags. They almost always match the AMC's, but confirm on
  the AMC site or your platform before investing.
- **Funds** are discovered automatically by name each run, so new US-focused
  launches get picked up. Debt/treasury and "ex-US" funds are excluded.
- A fund that looks like it flipped is re-fetched after 10s before alerting,
  so a one-off API glitch can't trigger an alert.
- `data/status.json` holds the latest state; `data/history.csv` logs every change.

## Setup

Repo secrets (Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN`: from @BotFather
- `TELEGRAM_CHAT_ID`: your id from @userinfobot (start your bot once first)

## Run locally

```
python watch.py daily --no-send --no-write   # dry run, prints the summary
python watch.py                               # today's summary, if not yet sent
```

Stdlib-only Python 3.8+. No dependencies.
