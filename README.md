<div align="center">

# 🍷 Apple LA Stock Monitor

**Quiet until it matters.**

Burgundy iPhone pickup alerts · Python standard library · Discord

</div>

## What it watches

| Configuration | Exact Apple SKU |
| :--- | :--- |
| iPhone 18 Pro Max · 256 GB · Burgundy | `MJW64LL/A` |
| iPhone 18 Pro Max · 512 GB · Burgundy | `MJWA4LL/A` |

**10 stores:** Sherman Oaks, Century City, Beverly Center, The Grove, Third Street Promenade, Topanga, Northridge, Glendale Galleria, The Americana at Brand, and Tower Theatre. Pasadena and other unlisted stores are excluded.

## How an alert qualifies

1. Query both SKUs around `90077` in a single primary pickup request.
2. Validate Apple's product names and inventory status for every one of the **20 SKU/store pairs**. Query a missing store directly instead of treating it as sold out.
3. For a positive candidate, try Apple's `fulfillment-messages` endpoint. An explicit unavailable result suppresses that pair's alert.
4. If fulfillment is blocked or omits the store, require a **second, direct-store pickup request** to report the exact pair available. The message identifies this fallback accurately.
5. Send one Burgundy-themed Discord embed with **`@everyone`**, storage, store, the latest pickup quote, check time, and verification method.

These endpoints are both Apple's storefront, not independent inventory providers. Two checks cannot reserve stock or guarantee that it remains available at checkout.

> [!IMPORTANT]
> Apple's fulfillment endpoint returned HTTP 541 from a hosted runner during diagnostics. The fallback does not bypass that restriction. It uses the already-working pickup endpoint and labels the confirmation accordingly.

## Schedule and proof

The workflow is configured for **every five minutes** at minutes `02, 07, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57`, around the clock. It also runs after monitor/code changes on `main`, and supports manual runs.

A cron line is **configuration, not proof of execution**. In **Actions → Apple Stock Monitor**, look for runs whose trigger is **schedule**. Every completed check writes a summary containing all 20 statuses, candidate and confirmed counts, and the notification decision. A manual or push-triggered success does not prove scheduling works.

GitHub documents scheduled workflows as best-effort: runs can start late or be dropped under load. No exact five-minute timing or zero-missed-restock guarantee is possible with this scheduler. Public-repository schedules can also be disabled after 60 days without repository activity. See [GitHub's scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Setup

Store a channel webhook as the repository Actions secret **`DISCORD_WEBHOOK_URL`** under **Settings → Secrets and variables → Actions**. Never commit the secret. Automatic alerts explicitly permit `@everyone`; member notification settings can still affect notifications.

For a **real live report**, open **Actions → Apple Stock Monitor → Run workflow**, select `main`, and enable `test_webhook`. It queries Apple and posts the real result even if there are no candidates. It does not mention anyone or load/save automatic alert state. If the API cannot be checked, the run fails instead of sending a misleading no-stock message.

Discord requests use `wait=true`. A successful send records the returned **Discord message ID** in the job log, without exposing the webhook token.

## Quiet, small, and defensive

Normal complete no-stock checks make **one Apple request**, with no pip install, browser, database, or server. Extra GETs happen only for missing coverage, candidates, or one bounded transient retry.

Notification state stores individual SKU/store pairs, not a hash of the entire stock list. Identical stock does not re-ping. One store selling out does not cause a ping for another store. A pair becomes eligible for a new alert after primary inventory explicitly reports it unavailable and it subsequently returns.

API/JSON/schema errors fail the run and preserve state. A webhook failure is not marked delivered. Runs are serialized rather than cancelling an alert midway. Manual reports skip the cache entirely. The job requires only repository contents read access.

The GitHub cache is best-effort storage: eviction, a cleared cache, or a crash after Discord accepted a message but before state was saved can cause a repeat alert. This is not an exactly-once messaging system. Migrating the old hash-based state may announce currently available stock once again.

## Local use

```bash
# Run the offline regression suite; no network access or webhook is needed.
python3 -m unittest discover -s tests -v

# Supply the webhook through your environment, never the source file.
export DISCORD_WEBHOOK_URL='your-private-discord-webhook-url'

# Perform a real manual report without changing the automatic alert history.
TEST_WEBHOOK=true python3 monitor.py

# Perform one normal automatic check.
TEST_WEBHOOK=false python3 monitor.py
```

## Repository

```text
.github/workflows/monitor.yml  Five-minute configuration, tests, manual mode, cache
monitor.py                    Validated Apple check, confirmation, Discord, state
tests/test_monitor.py         Offline parser, failure, fallback, and delivery tests
README.md                     Setup, behavior, and operational limitations
```

Unofficial project. Not affiliated with Apple or Discord. Confirm your order before travelling.
