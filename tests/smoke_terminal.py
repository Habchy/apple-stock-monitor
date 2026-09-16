"""Exercise the actual curses UI in a pseudo-terminal on macOS and Linux.

All Apple data in this test is an explicit offline fixture. Discord is disabled.
This is a UI test, not a stock check or a claim of current inventory availability.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Resize, navigate, pause, and exit a real terminal process without hanging CI."""
    master, slave = pty.openpty()
    process = None
    transcript = bytearray()

    def resize(rows: int, columns: int) -> None:
        # TIOCSWINSZ matches Terminal's resize event; SIGWINCH lets curses refresh it.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        if process is not None:
            process.send_signal(signal.SIGWINCH)

    def drain(seconds: float) -> None:
        # Read incrementally so a full pseudo-terminal buffer cannot stall the child.
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    transcript.extend(os.read(master, 65536))
                except OSError:
                    break

    try:
        with tempfile.TemporaryDirectory() as data_dir:
            resize(34, 104)
            child_code = """
import sys
sys.path.insert(0, 'tests')
import monitor, tui
from test_monitor import response
# Only the test process swaps network I/O for a complete no-stock fixture.
monitor.fetch_inventory = lambda: response()
raise SystemExit(tui.main(['--no-discord', '--no-keep-awake', '--data-dir', sys.argv[1]]))
"""
            environment = {**os.environ, 'TERM': 'xterm-256color', 'PYTHONUNBUFFERED': '1'}
            environment.pop('DISCORD_WEBHOOK_URL', None)
            process = subprocess.Popen([sys.executable, '-c', child_code, data_dir], cwd=ROOT,
                                       stdin=slave, stdout=slave, stderr=slave, env=environment)
            drain(1)
            os.write(master, b'p')
            drain(0.35)
            os.write(master, b'j')
            drain(0.35)
            resize(8, 40)
            drain(0.35)
            resize(34, 104)
            drain(0.35)
            os.write(master, b'q')
            drain(0.7)
            code = process.wait(timeout=5)
            text = transcript.decode('utf-8', errors='replace')
            assert code == 0, f'UI process failed: {text[-2000:]}'
            for expected in ('APPLE STOCK', 'Sherman Oaks', 'PAUSED', 'Resize', 'stopped'):
                assert expected in text, f'Missing terminal state {expected!r}'
            assert 'Traceback' not in text, 'Unexpected exception in terminal session'
            print('PASS: real curses initialization, live-state rendering, pause, navigation, resize, and clean quit')
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


if __name__ == '__main__':
    main()
