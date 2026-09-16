# 🍷 Apple LA Burgundy Stock Monitor

A tiny, dependency-free stock monitor for the **Burgundy iPhone 18 Pro Max** in Los Angeles. It checks Apple's live pickup inventory every five minutes and sends a polished Discord alert with `@everyone` when either target configuration appears.

## What it watches

| Model | Apple SKU |
| --- | --- |
| iPhone 18 Pro Max, 256 GB, Burgundy | `MJW64LL/A` |
| iPhone 18 Pro Max, 512 GB, Burgundy | `MJWA4LL/A` |

Stores: Sherman Oaks, Century City, Beverly Center, The Grove, Third Street Promenade, Topanga, Northridge, Glendale Galleria, The Americana at Brand, and Tower Theatre.

## Why this is tiny

There are **zero pip dependencies**. `monitor.py` uses only Python's standard library. One Apple request checks both SKUs, GitHub Actions handles the five-minute schedule, and a tiny cached fingerprint prevents duplicate alerts for the same availability state.

## Setup

1. Create a Discord webhook for the channel where you want alerts.
2. In this repository, open **Settings → Secrets and variables → Actions**.
3. Create a repository secret named **`DISCORD_WEBHOOK_URL`** and paste the webhook URL as its value.
4. Open **Actions → Apple Stock Monitor → Run workflow**.
5. Enable **`test_webhook`** and run it once. You should receive a clearly labeled test message in Discord.
6. That's it. The scheduled workflow checks automatically every five minutes.

> [!IMPORTANT]
> For `@everyone` to actually notify members, the Discord channel/webhook context must permit `@everyone` mentions.

## Alert behavior

When stock appears, Discord receives an `@everyone` message plus an embed listing the storage size, Apple Store, and Apple's current pickup quote. Identical inventory states are deduplicated. If stock disappears, the state resets, so a later restock can alert again.

The test workflow **never pretends a phone is in stock**. Its message explicitly says it is only a delivery test.

## Run locally

```bash
# The monitor needs a Discord webhook because its purpose is to alert.
export DISCORD_WEBHOOK_URL='your-webhook-url'
python3 monitor.py
```

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Files

```text
.
├── .github/workflows/monitor.yml   # 5-minute schedule + manual test trigger
├── monitor.py                      # Apple check, dedupe, Discord alert
├── tests/test_monitor.py           # Small behavior test suite
├── .gitignore
└── README.md
```

## Notes

GitHub scheduled workflows can occasionally start later than the exact cron time during periods of high Actions load. The monitor checks Apple's live pickup endpoint when each run actually starts.

This project is unofficial and is not affiliated with Apple or Discord.
