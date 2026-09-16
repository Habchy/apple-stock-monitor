# 🍷 Apple LA Burgundy Stock Monitor

A tiny, dependency-free stock monitor for the **Burgundy iPhone 18 Pro Max** in Los Angeles. It checks Apple's live pickup inventory every five minutes and sends a polished Discord alert with `@everyone` when either target configuration is independently confirmed available by two Apple storefront endpoints.

## What it watches

| Model | Apple SKU |
| --- | --- |
| iPhone 18 Pro Max, 256 GB, Burgundy | `MJW64LL/A` |
| iPhone 18 Pro Max, 512 GB, Burgundy | `MJWA4LL/A` |

Stores: Sherman Oaks, Century City, Beverly Center, The Grove, Third Street Promenade, Topanga, Northridge, Glendale Galleria, The Americana at Brand, and Tower Theatre.

## Two-source Apple verification

The monitor deliberately uses Apple's storefront data in two stages:

1. **Primary discovery:** `pickup-message` checks both Burgundy SKUs in one request.
2. **Confirmation:** if the primary endpoint reports a candidate, `fulfillment-messages` independently checks that exact SKU.
3. An automatic alert is sent only when the **same SKU and store** have `pickupDisplay == available` on both Apple responses.

This stays efficient because normal no-stock runs make only the single primary request. The extra Apple request happens only when there is something worth verifying.

There are **zero pip dependencies**. `monitor.py` uses only Python's standard library. GitHub Actions handles the five-minute schedule, and a tiny cached fingerprint prevents duplicate alerts for the same confirmed availability state.

## Setup

1. Create a Discord webhook for the channel where you want alerts.
2. In this repository, open **Settings → Secrets and variables → Actions**.
3. Create a repository secret named **`DISCORD_WEBHOOK_URL`** and paste the webhook URL as its value.
4. That's it. The scheduled workflow checks automatically every five minutes.

> [!IMPORTANT]
> For automatic stock alerts, `@everyone` must be permitted in the target Discord channel for the mention to actually notify members.

## Automatic alert behavior

When stock is cross-verified, Discord receives an `@everyone` message plus an embed listing the storage size, Apple Store, and Apple's current pickup quote. Identical confirmed inventory states are deduplicated. If confirmed stock disappears, the state resets, so a later restock can alert again.

Scheduled checks stay completely silent when there is no new confirmed availability.

## Manual live inventory check

Want to see what Apple is returning right now without waiting for a restock?

1. Open **Actions → Apple Stock Monitor → Run workflow**.
2. Enable **`test_webhook`**.
3. Run the workflow.

This mode is **not simulated**. It performs the same live Apple discovery and cross-verification used by the automatic monitor, then always posts the current confirmed result to Discord.

If confirmed stock exists, the Discord embed lists the real storage size, store, and Apple pickup quote. If nothing is confirmed, it says that no monitored Burgundy stock is currently confirmed.

Manual checks are clearly labeled **Manual Live Check**, never alter the automatic deduplication state, and deliberately do **not** ping `@everyone`.

## Run locally

```bash
# The monitor needs a Discord webhook because its purpose is to alert.
export DISCORD_WEBHOOK_URL='your-webhook-url'
python3 monitor.py
```

Run a manual live inventory report locally:

```bash
export TEST_WEBHOOK=true
python3 monitor.py
```

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Files

```text
.
├── .github/workflows/monitor.yml   # 5-minute schedule + manual live check trigger
├── monitor.py                      # Apple discovery, verification, dedupe, Discord alert
├── tests/test_monitor.py           # Behavior and cross-verification tests
├── .gitignore
└── README.md
```

## Notes

GitHub scheduled workflows can occasionally start later than the exact cron time during periods of high Actions load. The monitor checks Apple's live storefront endpoints when each run actually starts.

This project is unofficial and is not affiliated with Apple or Discord.
