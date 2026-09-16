"""Mac terminal regressions. All network boundaries use fixtures, never real alerts."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import monitor
from test_monitor import response, record, SKU, SKU2, WEBHOOK

try:
    import tui
except ModuleNotFoundError:
    tui = None


class TUITests(unittest.TestCase):
    """Verify timer, state, permissions, and display without needing a terminal."""

    def setUp(self):
        """Keep each test's local configuration isolated from the developer's home."""
        self.assertIsNotNone(tui, 'The local TUI has not been implemented yet')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / 'state.json'

    def check(self, hits=(), manual=False, webhook=WEBHOOK, delivery_error=None):
        """Run the real local check orchestration with isolated Apple/Discord I/O."""
        with patch.object(monitor, 'fetch_inventory', return_value=response(hits)), \
             patch.object(monitor, 'get_confirmed_inventory', return_value=[record(*p) for p in hits]), \
             patch.object(monitor, 'send_discord', side_effect=delivery_error, return_value='777') as send:
            result = tui.perform_check(webhook, self.state, manual)
        return result, send

    def test_first_check_is_immediate_and_next_is_five_minutes(self):
        """A local deadline replaces the unreliable remote cron trigger."""
        clock = tui.Schedule(300)
        self.assertTrue(clock.ready(100))
        clock.start(100)
        self.assertFalse(clock.ready(101))
        clock.finish(102)
        self.assertFalse(clock.ready(399))
        self.assertTrue(clock.ready(400))

    def test_slow_check_does_not_start_catchup_burst(self):
        """An overdue cycle schedules forward instead of overlapping network calls."""
        clock = tui.Schedule(300)
        clock.start(100)
        clock.finish(800)
        self.assertFalse(clock.ready(800))
        self.assertGreater(clock.due, 800)

    def test_pause_prevents_checks_and_resume_refreshes(self):
        """Pause is explicit; resuming requests an immediate fresh observation."""
        clock = tui.Schedule(300)
        clock.toggle_pause()
        self.assertFalse(clock.ready(1000))
        clock.toggle_pause()
        self.assertTrue(clock.ready(1000))

    def test_no_stock_stays_quiet_and_covers_twenty_cells(self):
        """An ordinary complete no-stock response never sends a Discord message."""
        result, send = self.check()
        self.assertIsNone(result.error)
        self.assertEqual(len(result.observations), 20)
        send.assert_not_called()

    def test_new_stock_sends_then_deduplicates_after_restart(self):
        """A durable local state file suppresses repeated pings across launches."""
        first, send = self.check([(SKU, 'R148')])
        self.assertEqual(first.receipt, '777')
        self.assertEqual(send.call_args.args[1]['content'], '@everyone')
        second, send = self.check([(SKU, 'R148')])
        send.assert_not_called()
        self.assertTrue(self.state.exists())

    def test_stock_disappearing_then_returning_alerts_again(self):
        """Only a real unavailable observation clears the pair's announcement."""
        self.check([(SKU, 'R148')])
        self.check()
        result, send = self.check([(SKU, 'R148')])
        self.assertEqual(send.call_count, 1)

    def test_manual_live_report_always_sends_without_ping_or_state_write(self):
        """A requested report reads Apple even with no stock and cannot consume an alert."""
        self.state.write_text('preserve this state')
        result, send = self.check(manual=True)
        self.assertEqual(result.receipt, '777')
        self.assertEqual(send.call_args.args[1]['allowed_mentions'], {'parse': []})
        self.assertEqual(self.state.read_text(), 'preserve this state')

    def test_delivery_failure_preserves_inventory_and_notification_state(self):
        """A webhook outage must not erase real inventory or mark the alert delivered."""
        self.check()
        before = self.state.read_bytes()
        result, send = self.check([(SKU, 'R148')], delivery_error=RuntimeError('Discord rejected HTTP 429'))
        self.assertIsNone(result.error)
        self.assertIn('429', result.delivery_error)
        self.assertEqual(len(result.confirmed), 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_network_error_does_not_claim_zero_stock(self):
        """Transport errors remain errors, not successful all-unavailable reports."""
        self.state.write_text('old state')
        with patch.object(monitor, 'fetch_inventory', side_effect=RuntimeError('Apple request failed')):
            result = tui.perform_check(WEBHOOK, self.state)
        self.assertIn('Apple', result.error)
        self.assertIsNone(result.observations)
        self.assertEqual(self.state.read_text(), 'old state')

    def test_local_only_never_sends_or_marks_discord_delivered(self):
        """Leaving the webhook blank is useful and must not suppress future Discord alerts."""
        result, send = self.check([(SKU, 'R148')], webhook='')
        self.assertIsNone(result.error)
        send.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_cancel_during_fetch_prevents_discord_post(self):
        """Quitting requests cancellation before any later network alert is initiated."""
        stop = threading.Event()
        stop.set()
        with patch.object(monitor, 'fetch_inventory', return_value=response()), \
             patch.object(monitor, 'send_discord') as send:
            tui.perform_check(WEBHOOK, self.state, manual=True, stop=stop)
        send.assert_not_called()

    def test_secrets_are_saved_owner_only(self):
        """Configuration permissions are checked on the actual filesystem."""
        path = self.root / 'config.json'
        tui.atomic_json(path, {'webhook': WEBHOOK})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())['webhook'], WEBHOOK)

    def test_invalid_config_error_never_echoes_its_contents(self):
        """A corrupt secret file cannot leak into the terminal or exception text."""
        path = self.root / 'config.json'
        path.write_text('bad SECRET_TOKEN')
        with self.assertRaises(RuntimeError) as caught:
            tui.read_config(path)
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))

    def test_invalid_webhook_is_rejected_without_echoing_it(self):
        """Only expected Discord HTTPS webhook destinations are accepted."""
        self.assertFalse(tui.valid_webhook('https://evil.example/SECRET_TOKEN'))
        self.assertTrue(tui.valid_webhook(WEBHOOK))

    def test_second_instance_cannot_use_same_notification_state(self):
        """An advisory lock prevents two local windows from generating duplicate alerts."""
        with tui.instance_lock(self.root / 'instance.lock'):
            with self.assertRaises(RuntimeError):
                with tui.instance_lock(self.root / 'instance.lock'):
                    self.fail('The second instance must not acquire the lock')

    def test_failed_check_marks_last_good_data_stale(self):
        """Previous stock can remain on screen only with a clear stale-data warning."""
        view = tui.View()
        good, _ = self.check()
        view.accept(good)
        view.accept(tui.Outcome(error='Apple unreachable'))
        self.assertIsNotNone(view.last_good)
        self.assertTrue(view.stale(300))
        self.assertIn('STALE', tui.frame_text(view, 100, 34))

    def test_unconfirmed_candidate_is_never_displayed_as_available(self):
        """A primary hit that fails confirmation has a distinct UI label."""
        view = tui.View()
        result, _ = self.check([(SKU, 'R148')])
        result.confirmed = []
        view.accept(result)
        self.assertEqual(view.cell(SKU, 'R148')[0], 'UNCONFIRMED')

    def test_tiny_terminal_has_a_resize_message_and_quit_hint(self):
        """Small windows degrade gracefully rather than crashing curses drawing."""
        text = tui.frame_text(tui.View(), 40, 8)
        self.assertIn('Resize', text)
        self.assertIn('Q', text)

    def test_untrusted_text_has_no_terminal_escape_sequences(self):
        """Apple quote text is data, never terminal control instructions."""
        self.assertNotIn('\x1b', tui.clean('hello\x1b[31mworld'))
        self.assertNotIn('\n', tui.clean('hello\nworld'))

    def test_only_one_worker_can_run_at_a_time(self):
        """Key mashing cannot create simultaneous inventory requests or duplicate sends."""
        event = threading.Event()
        def delayed(*args, **kwargs):
            event.wait(1)
            return tui.Outcome()
        with patch.object(tui, 'perform_check', side_effect=delayed):
            worker = tui.Worker('', self.state)
            self.assertTrue(worker.start())
            self.assertFalse(worker.start())
            event.set()
            worker.thread.join(2)
        self.assertIsNotNone(worker.poll())


if __name__ == '__main__':
    unittest.main()
