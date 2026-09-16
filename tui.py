#!/usr/bin/env python3
"""Local Apple stock dashboard. Python 3.10+, standard library only, no remote timer.

The main thread owns curses and the local deadline. A single worker performs the
existing validated Apple checks and Discord delivery. Keeping those responsibilities
separate leaves the UI responsive during a timeout and prevents overlapping polls.
"""
from __future__ import annotations

import argparse
from collections import deque
import contextlib
import curses
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
import getpass
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlsplit

import monitor

# All local data lives outside the Git checkout, so updates cannot commit a secret
# or erase notification history. The app never reads a GitHub secret or account.
DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'AppleStockMonitor'
DEFAULT_INTERVAL = 300
NORMAL, DIM, ACCENT, GOOD, WARN, BAD, HEADING, SELECTED = range(8)


def clean(text: object) -> str:
    """Treat API text as data: strip control/bidi characters and flatten whitespace."""
    normalized = unicodedata.normalize('NFKC', str(text))
    return ' '.join(''.join(c for c in normalized if not unicodedata.category(c).startswith('C')).split())


def clip(text: object, width: int) -> str:
    """Clip by terminal cells, not bytes, so quotes cannot overflow a resized window."""
    result, used = [], 0
    for char in clean(text):
        cells = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in 'WF' else 1)
        if used + cells > max(0, width):
            break
        result.append(char)
        used += cells
    return ''.join(result)


def atomic_json(path: Path, data: dict) -> None:
    """Save complete owner-only JSON atomically; never leave a partially written secret."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def valid_webhook(value: str) -> bool:
    """Validate the destination without logging or echoing its private token."""
    try:
        url = urlsplit(value)
        return bool(url.scheme == 'https' and
                    url.netloc in ('discord.com', 'canary.discord.com', 'ptb.discord.com') and
                    re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[\w-]+', url.path))
    except ValueError:
        return False


def read_config(path: Path) -> dict:
    """An absent config is first launch; corrupt settings are not silently overwritten."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('webhook'), str):
            raise ValueError
        if data['webhook'] and not valid_webhook(data['webhook']):
            raise ValueError
        # Repair overly broad permissions on an existing local configuration.
        path.chmod(0o600)
        return data
    except (ValueError, TypeError, OSError):
        raise RuntimeError('Cannot read local settings. Run with --setup to replace them.') from None


def configure(path: Path) -> dict:
    """Accept a hidden webhook once, or a blank value for a fully local dashboard."""
    print('\nAPPLE STOCK / LOCAL WATCH\n')
    print('Paste your Discord webhook below. Input is hidden, including while pasting.')
    print('It is saved locally with owner-only permissions, not encrypted or committed.')
    print('Press Return without a URL to run without Discord.\n')
    while True:
        value = getpass.getpass('Discord webhook: ').strip()
        if not value or valid_webhook(value):
            settings = {'webhook': value}
            atomic_json(path, settings)
            return settings
        print('That is not a supported Discord webhook URL. Please try again.')


@contextlib.contextmanager
def instance_lock(path: Path):
    """Hold one OS lock per data directory, avoiding duplicate app instances and pings."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Apple Stock Monitor is already running. Use its existing Terminal window.') from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        # Closing releases the lock, including after a crash. Keep the file itself:
        # unlinking a live lock file could allow two processes to lock different inodes.
        os.close(descriptor)


@contextlib.contextmanager
def keep_awake(enabled: bool):
    """Use macOS's process-bound idle-sleep assertion, without changing system settings."""
    child = None
    if enabled and sys.platform == 'darwin':
        try:
            child = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass  # The header will explicitly report that keep-awake is inactive.
    try:
        yield child
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


@dataclass
class Outcome:
    """Separate inventory failure from delivery failure so valid stock is never hidden."""
    observations: dict | None = None
    confirmed: list[dict] = field(default_factory=list)
    candidates: int = 0
    checked_at: float = 0
    duration: float = 0
    receipt: str | None = None
    error: str | None = None
    delivery_error: str | None = None
    manual: bool = False
    log: list[str] = field(default_factory=list)


def _announce(result: Outcome, webhook: str, path: Path) -> str | None:
    """Send new pairs once; preserve state on failure and never consume it for manual reports."""
    if result.manual:
        return monitor.send_discord(webhook, monitor.manual_discord_payload(result.confirmed, result.candidates))
    previous = set()
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if saved['version'] != 2 or not isinstance(saved['announced'], list) or any(not isinstance(v, str) for v in saved['announced']):
                raise ValueError
            previous = set(saved['announced'])
        except (ValueError, TypeError, KeyError):
            raise RuntimeError('Local notification state is corrupt; it was not reset.') from None
    confirmed = {monitor._pair(row) for row in result.confirmed}
    receipt = None
    if confirmed - previous:
        # The production formatter includes every confirmed store for both target
        # sizes, while only a new SKU/store pair makes it eligible for a ping.
        receipt = monitor.send_discord(webhook, monitor.discord_payload(result.confirmed))
    candidates = {monitor._pair(row) for row in result.observations.values() if row['status'] == 'available'}
    atomic_json(path, {'version': 2, 'announced': sorted((previous & candidates) | confirmed)})
    return receipt


def perform_check(webhook: str, state_path: Path, manual: bool = False,
                  stop: threading.Event | None = None) -> Outcome:
    """Run the same real Apple parser/confirmation as the original monitor, once.

    This function runs only in the single worker (or --once). Capturing the existing
    core's informational prints prevents them from corrupting the curses screen.
    The display thread never prints to stdout while a worker is active.
    """
    result = Outcome(manual=manual)
    started, transcript = time.monotonic(), io.StringIO()
    try:
        with contextlib.redirect_stdout(transcript):
            if stop is not None and stop.is_set():
                raise RuntimeError('Check stopped')
            primary = monitor.fetch_inventory()
            observed = monitor._observations(primary)
            expected = {(sku, store) for sku in monitor.PRODUCTS for store in monitor.STORES}
            if not expected.issubset(observed):
                raise RuntimeError('Inventory coverage incomplete; not an out-of-stock result')
            confirmed = monitor.get_confirmed_inventory(primary)
            result.observations = observed
            result.confirmed = confirmed
            result.candidates = sum(row['status'] == 'available' for row in observed.values())
            result.checked_at = time.time()
            # A local-only run must not mark a Discord alert delivered. Quitting
            # also prevents a new POST, although an already-started POST may finish.
            if webhook and not (stop is not None and stop.is_set()):
                try:
                    result.receipt = _announce(result, webhook, state_path)
                except (RuntimeError, OSError) as error:
                    result.delivery_error = clean(error)
    except (RuntimeError, OSError) as error:
        result.error = clean(error)
    except Exception as error:
        # Do not echo arbitrary exceptions that might contain request objects or
        # secret URLs. The class identifies an unexpected bug without leaking data.
        result.error = f'Unexpected {type(error).__name__} during check; retrying next cycle'
    result.duration = time.monotonic() - started
    result.log = [clean(line) for line in transcript.getvalue().splitlines() if line.strip()]
    return result


@dataclass
class Schedule:
    """Local start-to-start timer. No remote service, catch-up bursts, or overlapping jobs."""
    interval: int = DEFAULT_INTERVAL
    due: float = 0
    started: float = 0
    busy: bool = False
    paused: bool = False

    def ready(self, now: float) -> bool:
        """The first check is immediately due; a paused or busy job cannot auto-start."""
        return not self.busy and not self.paused and now >= self.due

    def start(self, now: float) -> None:
        """Reserve the single check slot before creating its worker thread."""
        self.busy, self.started = True, now

    def finish(self, now: float) -> None:
        """Count normal intervals from start; a slow check gets a short recovery gap."""
        self.busy = False
        self.due = max(self.started + self.interval, now + 5)

    def toggle_pause(self) -> None:
        """Resuming makes a fresh check due immediately rather than displaying old data."""
        self.paused = not self.paused
        if not self.paused:
            self.due = 0


class Worker:
    """Only this thread touches the network; only the main thread touches curses."""
    def __init__(self, webhook: str, state: Path):
        self.webhook, self.state = webhook, state
        self.thread = None
        self.results = queue.Queue(maxsize=1)
        self.stop = threading.Event()

    def start(self, manual: bool = False) -> bool:
        """Ignore repeated keypresses until the existing check and its result finish."""
        if self.stop.is_set() or (self.thread is not None and self.thread.is_alive()) or not self.results.empty():
            return False
        def work():
            self.results.put(perform_check(self.webhook, self.state, manual, self.stop))
        self.thread = threading.Thread(target=work, name='apple-inventory', daemon=True)
        self.thread.start()
        return True

    def poll(self) -> Outcome | None:
        """Transfer a finished observation without blocking keyboard input."""
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None


@dataclass
class View:
    """UI state retains the last successful table but prominently labels stale data."""
    schedule: Schedule = field(default_factory=Schedule)
    last_good: Outcome | None = None
    error: str | None = None
    delivery_error: str | None = None
    selected: int = 0
    checks: int = 0
    failures: int = 0
    discord: bool = False
    awake: bool = False
    closing: bool = False
    events: deque = field(default_factory=lambda: deque(maxlen=50))
    seen: set = field(default_factory=set)

    def note(self, text: str) -> None:
        """Keep a bounded activity feed; it never contains a webhook or authorization token."""
        self.events.appendleft((datetime.now().strftime('%H:%M:%S'), clean(text)))

    def accept(self, result: Outcome) -> bool:
        """Return whether a new local stock hit deserves a bell; errors never clear history."""
        self.checks += 1
        if result.error:
            self.error = result.error
            self.failures += 1
            self.note('CHECK FAILED: ' + result.error)
            return False
        self.last_good, self.error, self.delivery_error = result, None, result.delivery_error
        hits = {monitor._pair(row) for row in result.confirmed}
        new_hit = bool(hits - self.seen) and not result.manual
        if not result.manual:
            candidates = {monitor._pair(row) for row in result.observations.values() if row['status'] == 'available'}
            self.seen = (self.seen & candidates) | hits
        if result.receipt:
            self.note(('Manual report' if result.manual else 'Stock alert') + f' delivered to Discord (ID {result.receipt})')
        elif result.delivery_error:
            self.note('DISCORD ERROR: ' + result.delivery_error)
        else:
            self.note(f'Checked 20/20 cells in {result.duration:.1f}s. {len(hits)} confirmed. No Discord message.')
        return new_hit

    def stale(self, interval: int | None = None) -> bool:
        """A failed, paused, or overdue snapshot is not presented as current stock."""
        age = time.time() - self.last_good.checked_at if self.last_good else 0
        return bool(self.error or self.schedule.paused or age > (interval or self.schedule.interval) + 30)

    def cell(self, sku: str, store: str) -> tuple[str, int]:
        """Primary-only hits are UNCONFIRMED, never the green AVAILABLE state."""
        if self.last_good is None:
            return 'Not checked', DIM
        info = self.last_good.observations.get((sku, store))
        if info is None:
            return 'Unknown', WARN
        if any(row['sku'] == sku and row['store_id'] == store for row in self.last_good.confirmed):
            return 'AVAILABLE', GOOD
        return ('Unavailable', DIM) if info['status'] == 'unavailable' else ('UNCONFIRMED', WARN)


def frame(view: View, width: int, height: int) -> list[tuple[int, int, str, int]]:
    """Build a testable layout from real state; no network or terminal calls happen here."""
    spans = []
    def put(y, x, text, style=NORMAL):
        if 0 <= y < height and 0 <= x < width - 1:
            spans.append((y, x, clip(text, width - x - 1), style))
    if width < 76 or height < 28:
        put(1, 1, 'APPLE STOCK / LOCAL WATCH', ACCENT)
        put(3, 1, 'Resize to at least 76 columns x 28 rows.', WARN)
        put(5, 1, 'Checks continue. Q quits; P pauses.', DIM)
        return spans
    now, stale = time.monotonic(), view.stale()
    status = 'STOPPING' if view.closing else ('PAUSED' if view.schedule.paused else ('CHECK FAILED' if view.error else 'WATCHING'))
    put(1, 3, 'APPLE STOCK', ACCENT)
    put(1, 19, '/  LOCAL WATCH', HEADING)
    put(1, width - 19, status, BAD if view.error else (WARN if view.schedule.paused else GOOD))
    put(2, 3, 'iPhone 18 Pro Max  /  Burgundy  /  Los Angeles', DIM)
    countdown = 'Checking Apple...' if view.schedule.busy else ('Paused' if view.schedule.paused else f'{max(0, math.ceil(view.schedule.due - now)) // 60:02}:{max(0, math.ceil(view.schedule.due - now)) % 60:02}')
    last = datetime.fromtimestamp(view.last_good.checked_at).strftime('%H:%M:%S') if view.last_good else 'Not yet'
    put(4, 3, f'NEXT  {countdown}', HEADING)
    put(4, 32, f'LAST  {last}', DIM)
    put(4, 55, f'INTERVAL  {view.schedule.interval}s', DIM)
    put(5, 3, ('Discord enabled' if view.discord else 'LOCAL ONLY: Discord not configured') +
        '  |  ' + ('Idle sleep prevented' if view.awake else 'Keep-awake off'), DIM)
    put(6, 2, '-' * (width - 4), DIM)
    put(7, 4, 'APPLE STORE', HEADING)
    put(7, 39, '256 GB', HEADING)
    put(7, 57, '512 GB', HEADING)
    put(8, 2, '-' * (width - 4), DIM)
    for index, (store, name) in enumerate(monitor.STORES.items()):
        put(9 + index, 3, ('> ' if index == view.selected else '  ') + name, SELECTED if index == view.selected else NORMAL)
        for x, sku in zip((39, 57), monitor.PRODUCTS):
            label, color = view.cell(sku, store)
            put(9 + index, x, label, DIM if stale else color)
    put(19, 2, '-' * (width - 4), DIM)
    if stale:
        put(20, 3, 'STALE / LAST KNOWN DATA. ' + (view.error or 'Paused or overdue; not a fresh stock claim.'), BAD)
    elif view.last_good:
        put(20, 3, f'20/20 checked  |  {len(view.last_good.confirmed)} confirmed  |  {view.checks} checks  |  {view.failures} failed', GOOD if view.last_good.confirmed else DIM)
    else:
        put(20, 3, 'Waiting for the first live Apple response. No stock assumptions.', DIM)
    store = list(monitor.STORES)[view.selected]
    put(21, 3, monitor.STORES[store] + ' / APPLE PICKUP QUOTES' + (' / LAST KNOWN' if stale else ''), HEADING)
    for y, (sku, size) in zip((22, 23), monitor.PRODUCTS.items()):
        info = view.last_good.observations.get((sku, store), {}) if view.last_good else {}
        confirmed = next((row for row in view.last_good.confirmed if row['sku'] == sku and row['store_id'] == store), {}) if view.last_good else {}
        put(y, 3, size + '  ' + (confirmed or info).get('pickup', 'Not checked yet'), DIM)
    if view.delivery_error:
        put(24, 3, 'DISCORD ERROR: ' + view.delivery_error, BAD)
    else:
        put(24, 3, 'Unconfirmed = primary hit not confirmed. Stock is not reserved.', DIM)
    put(26, 3, 'ACTIVITY', HEADING)
    for y, (stamp, text) in zip(range(27, height - 3), view.events):
        put(y, 3, stamp + '  ' + text, WARN if 'ERROR' in text or 'FAILED' in text else DIM)
    put(height - 3, 2, '-' * (width - 4), DIM)
    put(height - 2, 3, 'R Refresh  M Live report  P Pause  Up/Down Select  O Apple  Q Quit', HEADING)
    return spans


def frame_text(view: View, width: int, height: int) -> str:
    """Render the same layout as plain text for reproducible UI regression checks."""
    lines = [[' '] * width for _ in range(height)]
    for y, x, text, _ in frame(view, width, height):
        for offset, char in enumerate(text):
            if x + offset < width:
                lines[y][x + offset] = char
    return '\n'.join(''.join(line).rstrip() for line in lines)


def palette() -> dict[int, int]:
    """Prefer a burgundy accent in 256-color terminals; support basic or no-color terminals."""
    attributes = {style: 0 for style in range(8)}
    attributes[DIM], attributes[HEADING] = curses.A_DIM, curses.A_BOLD
    if not curses.has_colors():
        return attributes
    try:
        curses.use_default_colors()
        shades = (252, 245, 175, 114, 221, 203, 255, 175) if curses.COLORS >= 256 else (7, 7, 5, 2, 3, 1, 7, 5)
        for style, color in enumerate(shades):
            curses.init_pair(style + 1, color, -1)
            attributes[style] = curses.color_pair(style + 1)
        for style in (ACCENT, GOOD, BAD, HEADING, SELECTED):
            attributes[style] |= curses.A_BOLD
    except curses.error:
        pass
    return attributes


def dashboard(screen, webhook: str, state_path: Path, interval: int, awake_process=None) -> None:
    """Render about once per second while servicing keys independently of network requests."""
    view = View(schedule=Schedule(interval), discord=bool(webhook))
    view.note('Local timer started. First check is immediate; no GitHub scheduler.')
    worker, colors = Worker(webhook, state_path), palette()
    screen.timeout(250)
    screen.keypad(True)
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    last_draw, last_mono, last_wall = -1, time.monotonic(), time.time()
    quit_started = None
    try:
        while True:
            now, wall = time.monotonic(), time.time()
            # macOS clocks may treat suspend differently. A large clock gap requests
            # a fresh observation after wake rather than trusting the pre-sleep table.
            if abs((wall - last_wall) - (now - last_mono)) > 30 or now - last_mono > 30:
                view.schedule.due = 0
                view.note('Wake or clock gap detected; refreshing live inventory.')
            last_mono, last_wall = now, wall
            result = worker.poll()
            if result is not None:
                if view.accept(result):
                    with contextlib.suppress(curses.error):
                        curses.beep()
                view.schedule.finish(now)
                last_draw = -1
            if not view.closing and view.schedule.ready(now) and worker.start():
                view.schedule.start(now)
            if view.closing and (worker.thread is None or not worker.thread.is_alive() or now - quit_started > 16):
                break
            view.awake = awake_process is not None and awake_process.poll() is None
            tick = int(now * (2 if view.schedule.busy else 1))
            if tick != last_draw:
                screen.erase()
                height, width = screen.getmaxyx()
                for y, x, text, style in frame(view, width, height):
                    with contextlib.suppress(curses.error):
                        screen.addstr(y, x, text, colors[style])
                screen.refresh()
                last_draw = tick
            key = screen.getch()
            if key == -1:
                continue
            last_draw = -1
            if key in (ord('q'), ord('Q'), 27):
                if view.closing:
                    break
                view.closing, quit_started = True, time.monotonic()
                worker.stop.set()
                view.note('Stopping. Allowing an in-flight request to finish; Q again exits immediately.')
            elif not view.closing and key in (ord('p'), ord('P')):
                view.schedule.toggle_pause()
                view.note('Paused. Any in-flight check may finish.' if view.schedule.paused else 'Resumed. A fresh check is due.')
            elif not view.closing and key in (ord('r'), ord('R'), ord('m'), ord('M')):
                manual = key in (ord('m'), ord('M'))
                if manual and not webhook:
                    view.note('No Discord configured. Restart with --setup to add a webhook.')
                elif view.schedule.busy:
                    view.note('A check is already in progress; requests will not overlap.')
                elif now - view.schedule.started < 5:
                    view.note('Please wait a few seconds before another manual request.')
                elif worker.start(manual):
                    view.schedule.start(now)
            elif key in (curses.KEY_DOWN, ord('j')):
                view.selected = (view.selected + 1) % len(monitor.STORES)
            elif key in (curses.KEY_UP, ord('k')):
                view.selected = (view.selected - 1) % len(monitor.STORES)
            elif key in (ord('o'), ord('O')) and sys.platform == 'darwin':
                # Open Apple's normal purchase page; never reserve or buy automatically.
                with contextlib.suppress(OSError):
                    subprocess.Popen(['/usr/bin/open', 'https://www.apple.com/shop/buy-iphone'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        worker.stop.set()


def main(argv=None) -> int:
    """Configure locally, prevent duplicate instances, and restore Terminal on exit."""
    parser = argparse.ArgumentParser(description='Local Apple pickup stock TUI. No pip dependencies.')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='Seconds between checks (default 300; minimum 60)')
    parser.add_argument('--setup', action='store_true', help='Replace your locally saved Discord webhook')
    parser.add_argument('--no-discord', action='store_true', help='Local dashboard only; ignore any saved webhook')
    parser.add_argument('--no-keep-awake', action='store_true', help='Do not prevent macOS idle sleep')
    parser.add_argument('--once', action='store_true', help='One foreground check, print the result, then exit')
    parser.add_argument('--report', action='store_true', help='With --once, send a non-pinging live Discord report')
    parser.add_argument('--data-dir', type=Path, default=DATA_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 60 <= args.interval <= 86400:
        parser.error('--interval must be between 60 and 86400 seconds')
    if args.report and not args.once:
        parser.error('--report requires --once; use M inside the dashboard')
    if not args.once and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error('Run this in a Terminal window, not a redirected pipe. Use --once for non-interactive checks.')
    if not args.once and os.environ.get('TERM', '') in ('', 'dumb'):
        parser.error('A color-capable Terminal session is required for the dashboard.')
    try:
        with instance_lock(args.data_dir / 'instance.lock'):
            path = args.data_dir / 'config.json'
            if args.no_discord:
                webhook = ''
            elif args.setup:
                webhook = configure(path)['webhook']
            elif os.environ.get('DISCORD_WEBHOOK_URL'):
                webhook = os.environ['DISCORD_WEBHOOK_URL'].strip()
            else:
                settings = read_config(path)
                if not settings and not args.once:
                    settings = configure(path)
                webhook = settings.get('webhook', '')
            if webhook and not valid_webhook(webhook):
                raise RuntimeError('Discord webhook is invalid. Use --setup or correct your environment variable.')
            if args.report and not webhook:
                raise RuntimeError('A Discord webhook is required for --report. Use --setup first.')
            state = args.data_dir / 'state.json'
            if args.once:
                result = perform_check(webhook, state, args.report)
                view = View(discord=bool(webhook))
                view.accept(result)
                print(frame_text(view, 104, 34))
                return 1 if result.error or result.delivery_error else 0
            # wrapper restores echo/cursor modes on normal quit and on exceptions.
            with keep_awake(not args.no_keep_awake) as child:
                curses.wrapper(dashboard, webhook, state, args.interval, child)
        print('Apple Stock Monitor stopped. No further local checks will run.')
        return 0
    except KeyboardInterrupt:
        print('\nApple Stock Monitor stopped.')
        return 130
    except (RuntimeError, OSError, curses.error) as error:
        print('Apple Stock Monitor: ' + clean(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    # SIGTERM follows the same terminal-restoring path as Control-C. No service is
    # installed: closing Terminal or logging out stops this foreground application.
    def terminate(signum, current_frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
