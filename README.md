<div align="center">

# Apple Stock / Local Watch

**A quiet Mac terminal dashboard. No GitHub timer. No server. No pip dependencies.**

Burgundy iPhone pickup stock · 10 Los Angeles stores · Discord alerts

</div>

## Start on your Mac

You need **Python 3.10 or newer** with `curses`, plus an internet connection. The launcher checks for a compatible Python before starting. Python's standard macOS distribution includes the required modules; there is no app dependency installation.

```bash
# Clone to a new folder, then launch the local terminal dashboard.
git clone https://github.com/Habchy/apple-stock-monitor.git apple-stock-monitor-mac
cd apple-stock-monitor-mac
bash Start.command
```

Already have this repository checked out? Run `git pull --ff-only`, then `bash Start.command` from that folder. A downloaded ZIP also works: unzip it and run `bash Start.command` inside the extracted folder. The executable `Start.command` can also be opened from Finder when permitted by your Mac's security settings.

On first launch, paste your Discord webhook into the **hidden prompt**, then press Return. It is saved **only on your Mac**, outside the repository, in an owner-readable/writable configuration file. Input is not displayed while typing or pasting. This local file is **not encrypted**. Press Return without a webhook to use a local-only dashboard instead.

Your GitHub Actions secret is not copied to your Mac. Re-enter the webhook locally; do not put it in source code or paste it into a chat.

The first real Apple check starts immediately. The next is due **300 seconds after the previous check started**, using a timer in the running app. No cron activation or cloud scheduling is involved.

> [!IMPORTANT]
> Leave the Terminal session open and your Mac connected to the internet. For an unattended MacBook, use power and leave the lid open. The app prevents **idle system sleep** with the built-in `caffeinate -i -w <app-pid>`. It does not force the display to stay on, and it does not override lid-close sleep, an explicit Sleep command, logout, shutdown, or a dead battery. There is no background service or automatic login item.

## Inside the dashboard

A burgundy-accented stock table shows **both sizes at every store**, with a countdown, last successful check time, check/error counts, and an activity feed. Select a store to see Apple's pickup quotes. Resize Terminal freely; about **104 columns by 34 rows** is comfortable. Below 76 by 28, a resize message appears while polling continues.

| Key | Behavior |
| :--- | :--- |
| **R** | Refresh live inventory now. A newly confirmed hit can send the normal `@everyone` alert. |
| **M** | Make a fresh live check and always send its result to Discord, even without stock. No mentions, no change to automatic alert history. |
| **P** | Pause or resume polling. Resuming checks immediately. An already running check may finish. |
| **Up / Down** or **K / J** | Select a store and inspect its pickup quotes. |
| **O** | Open Apple's iPhone shopping page on macOS. It never orders automatically. |
| **Q** | Stop polling and quit. Allows an in-flight operation up to 16 seconds to finish; press Q again to exit immediately. Control-C also exits. |

**AVAILABLE** means a primary candidate passed a second availability check. **UNCONFIRMED** means the primary saw stock but confirmation did not agree. **Unavailable** is an explicit Apple status, not a guess. **STALE** labels the previous table after an error, during pause, or if it is overdue. The app retries on the next local interval without freezing the keyboard.

The app attempts a terminal bell for a new confirmed local hit. Whether it is audible depends on Terminal settings. Discord remains the remote notification path.

## Watched configurations

| Model | Exact Apple SKU |
| :--- | :--- |
| iPhone 18 Pro Max · 256 GB · Burgundy | `MJW64LL/A` |
| iPhone 18 Pro Max · 512 GB · Burgundy | `MJWA4LL/A` |

| Apple Store | Store ID |
| :--- | :--- |
| Sherman Oaks | `R148` |
| Century City | `R108` |
| Beverly Center | `R124` |
| The Grove | `R050` |
| Third Street Promenade | `R051` |
| Topanga | `R189` |
| Northridge | `R023` |
| Glendale Galleria | `R001` |
| The Americana at Brand | `R451` |
| Tower Theatre | `R720` |

Pasadena, other stores, and other colors are not alert targets.

## Same validated Apple checks

The TUI reuses `monitor.py`, not a mock or a separate stock feed. It batches both SKUs around `90077`, validates product identity and all **20 SKU/store cells**, and queries missing stores directly instead of silently treating them as unavailable.

A positive candidate is checked against `fulfillment-messages`. An explicit unavailable result suppresses the alert. If that endpoint is blocked or omits the store, the monitor requires another **direct-store pickup request** to report the pair available. The Discord embed states the actual verification method and Apple's latest quote. These are Apple storefront endpoints, not independent inventory providers or a reservation guarantee.

Normal complete no-stock checks make **one Apple request**. Missing coverage, confirmation, and one bounded transient GET retry can add requests. Network work runs in **one worker thread**, separate from the UI. Checks never overlap, and a delayed cycle does not cause a burst of catch-up requests. The app waits between checks rather than spinning its CPU.

## Discord and local state

Automatic messages use the existing Burgundy embed, list confirmed sizes/stores/pickup timing, and explicitly allow **`@everyone`**. No stock means no automatic message. Manual reports never allow mentions. Discord returns a message ID when it accepts a post; that receipt appears in the activity feed. Discord/channel/member settings still determine notification behavior.

Configuration and automatic alert history are stored under:

```text
~/Library/Application Support/AppleStockMonitor/
    config.json      Private webhook, file mode 0600
    state.json       Announced SKU/store pairs, file mode 0600
    instance.lock    OS lock preventing two local instances
```

The app persists individual pairs. Repeated availability does not re-ping; one store selling out does not re-ping another store. An explicit unavailable observation permits a later restock alert. API or delivery failure does not erase alert state. Local-only and manual-report checks do not mark Discord alerts delivered.

This is not exactly-once delivery: a crash after Discord accepts a message but before local state is saved, or an ambiguous network timeout, can cause a repeat later. Deleting the state file also resets deduplication. Local history is separate from the old GitHub cache.

## Options

```bash
# Replace the saved webhook using a hidden prompt.
bash Start.command --setup

# Watch locally without Discord or a secret prompt.
bash Start.command --no-discord

# Optional faster polling: every 60 seconds instead of the default 300.
bash Start.command --interval 60

# Leave your normal macOS idle-sleep settings untouched.
bash Start.command --no-keep-awake

# One real read-only inventory check for diagnosis; no Discord message.
python3 tui.py --once --no-discord

# One real non-pinging Discord report using the saved webhook.
python3 tui.py --once --report
```

`DISCORD_WEBHOOK_URL` can supply a webhook through the environment instead of the saved config. `--no-discord` overrides it. The TUI does not use the old `TEST_WEBHOOK` environment variable; **M** is the live-report control.

## Verification and GitHub

```bash
# All regression tests are offline; fixtures cannot send real notifications.
python3 -m unittest discover -s tests -v -b

# Exercise real curses initialization, keyboard handling, resizing, and shutdown
# inside a pseudo-terminal. Apple and Discord are explicitly mocked in this test.
python3 tests/smoke_terminal.py
```

CI runs these tests on macOS and Linux. The old automatic stock schedule and temporary scheduler control are removed. GitHub Actions is used for tests and an optional **manual-only** cloud stock check, not as the timer for the Mac app.

No signed macOS binary is bundled. This is reviewed Python source launched in Terminal. Apple's storefront API can change or block requests, so a successful launch is not a guarantee of future network access or checkout stock. Never disable TLS verification to hide a certificate error. With a python.org installation, complete its **Install Certificates.command** step if HTTPS is not working.

## Files

```text
Start.command                 Mac-friendly launcher; installs nothing
tui.py                        Local timer, dashboard, configuration, state, keep-awake
monitor.py                    Shared Apple validation, confirmation, Discord formatting
tests/test_monitor.py         Existing inventory and delivery regressions
tests/test_tui.py             Local scheduling, state, permissions, and UI tests
tests/smoke_terminal.py       Real pseudo-terminal smoke test
.github/workflows/ci.yml      macOS + Linux tests
.github/workflows/watch.yml   Optional manual-only cloud check
```

References: [Python curses](https://docs.python.org/3/howto/curses.html), [Python on macOS](https://docs.python.org/3/using/mac.html), [Apple's caffeinate manual](https://github.com/apple-oss-distributions/PowerManagement/blob/main/caffeinate/caffeinate.8), [Mac sleep behavior](https://support.apple.com/guide/mac-help/put-your-mac-to-sleep-or-wake-it-mh10330/mac).

Unofficial project. Not affiliated with Apple or Discord. Confirm an order before travelling.
