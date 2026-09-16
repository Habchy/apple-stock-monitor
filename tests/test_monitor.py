"""Offline regressions use observed Apple wrappers, never live stock or secrets."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import monitor

SKU = 'MJW64LL/A'
SKU2 = 'MJWA4LL/A'
# This intentionally fake URL is sufficient for payload tests and cannot send a real message.
WEBHOOK = 'https://discord.com/api/webhooks/123456789/test-only-not-a-real-token'


def response(hits=(), stores=None, fulfillment=False, products=None):
    """Build complete, minimal fixtures with Apple's actual nested message fields."""
    products = products or monitor.PRODUCTS
    stores = list(monitor.STORES) if stores is None else stores
    rows = []
    for store_id in stores:
        parts = {}
        for sku, size in products.items():
            available = (sku, store_id) in hits
            parts[sku] = {
                'partNumber': sku,
                'pickupDisplay': 'available' if available else 'unavailable',
                'messageTypes': {'regular': {
                    'storePickupProductTitle': f'iPhone\u00a018 Pro\u00a0Max {size.replace(" ", "")} Burgundy',
                    'storePickupQuote': 'Fri Sep 18 at Apple Sherman Oaks' if available else 'Currently unavailable',
                }},
            }
        rows.append({'storeNumber': store_id, 'storeName': store_id, 'partsAvailability': parts})
    body = {'content': {'pickupMessage': {'stores': rows}}} if fulfillment else {'stores': rows}
    return {'head': {'status': '200'}, 'body': body}


def record(sku=SKU, store='R148'):
    """A parsed available record used only in isolated formatting/state tests."""
    return {'sku': sku, 'store_id': store, 'storage': monitor.PRODUCTS[sku], 'store': 'Sherman Oaks', 'pickup': 'Fri Sep 18', 'verification': 'Fulfillment matched'}


class ParserTests(unittest.TestCase):
    """Fail closed on broken data rather than manufacturing an out-of-stock result."""

    def test_real_nested_primary_pickup_quote_is_preserved(self):
        found = monitor.extract_available(response([(SKU, 'R148')]))
        self.assertEqual(found[0]['pickup'], 'Fri Sep 18 at Apple Sherman Oaks')

    def test_missing_stores_is_an_error(self):
        with self.assertRaises(RuntimeError):
            monitor.extract_available({'head': {'status': '200'}, 'body': {}})

    def test_application_error_inside_http_success_is_an_error(self):
        data = response()
        data['head']['status'] = '541'
        with self.assertRaises(RuntimeError):
            monitor.extract_available(data)

    def test_wrong_product_title_is_not_burgundy_stock(self):
        data = response([(SKU, 'R148')], ['R148'])
        data['body']['stores'][0]['partsAvailability'][SKU]['messageTypes']['regular']['storePickupProductTitle'] = 'iPhone 18 Pro Max 256GB Black'
        with self.assertRaises(RuntimeError):
            monitor.extract_available(data)

    def test_unknown_inventory_status_is_not_unavailable(self):
        data = response(stores=['R148'])
        data['body']['stores'][0]['partsAvailability'][SKU]['pickupDisplay'] = 'new-unknown-state'
        with self.assertRaises(RuntimeError):
            monitor.extract_available(data)

    def test_unmonitored_store_and_product_do_not_alert(self):
        data = response([(SKU, 'R034')], ['R034'])
        self.assertEqual(monitor.extract_available(data), [])

    def test_fulfillment_real_nested_shape_is_supported(self):
        found = monitor.extract_fulfillment_available(response([(SKU, 'R148')], fulfillment=True))
        self.assertEqual(found[0]['pickup'], 'Fri Sep 18 at Apple Sherman Oaks')

    def test_missing_store_is_queried_directly(self):
        partial = response(stores=[s for s in monitor.STORES if s != 'R023'])
        requests = []
        def get(url):
            # The live boundary is replaced, but coverage and merging run normally.
            requests.append(parse_qs(urlsplit(url).query))
            return response(stores=['R023']) if 'store' in requests[-1] else partial
        with patch.object(monitor, '_fetch_json', side_effect=get):
            data = monitor.fetch_inventory()
        self.assertEqual(len(data['body']['stores']), 10)
        self.assertTrue(any(q.get('store') == ['R023'] for q in requests))


class ConfirmationTests(unittest.TestCase):
    """Exercise the confirmation decision without contacting Apple or Discord."""

    def test_no_stock_does_not_call_secondary(self):
        def forbidden(sku):
            self.fail('No candidate should mean no confirmation request')
        self.assertEqual(monitor.get_confirmed_inventory(response(), forbidden), [])

    def test_explicit_secondary_unavailable_is_not_overridden(self):
        data = response([(SKU, 'R148')])
        with patch.object(monitor, '_fetch_json', side_effect=AssertionError('No fallback on explicit unavailable')):
            result = monitor.get_confirmed_inventory(data, lambda sku: response(fulfillment=True))
        self.assertEqual(result, [])

    def test_blocked_secondary_uses_direct_store_recheck(self):
        def blocked(sku):
            raise RuntimeError('Apple fulfillment HTTP 541')
        with patch.object(monitor, '_fetch_json', return_value=response([(SKU, 'R148')], ['R148'])):
            result = monitor.get_confirmed_inventory(response([(SKU, 'R148')]), blocked)
        self.assertEqual([(r['sku'], r['store_id']) for r in result], [(SKU, 'R148')])
        self.assertIn('direct', result[0]['verification'].lower())

    def test_both_confirmation_paths_failing_is_not_no_stock(self):
        def blocked(sku):
            raise RuntimeError('Apple fulfillment HTTP 541')
        with patch.object(monitor, '_fetch_json', side_effect=RuntimeError('Apple primary unavailable')):
            with self.assertRaises(RuntimeError):
                monitor.get_confirmed_inventory(response([(SKU, 'R148')]), blocked)

    def test_confirmation_uses_fresh_pickup_quote(self):
        data = response([(SKU, 'R148')], fulfillment=True)
        for row in data['body']['content']['pickupMessage']['stores']:
            if row['storeNumber'] == 'R148':
                row['partsAvailability'][SKU]['messageTypes']['regular']['storePickupQuote'] = 'Sat Sep 19'
        found = monitor.get_confirmed_inventory(response([(SKU, 'R148')]), lambda sku: data)
        self.assertEqual(found[0]['pickup'], 'Sat Sep 19')


class DeliveryTests(unittest.TestCase):
    """Message formatting must distinguish manual output from real notifications."""

    def test_manual_empty_does_not_claim_second_endpoint_ran(self):
        payload = monitor.manual_discord_payload([])
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        text = json.dumps(payload).lower()
        self.assertNotIn('cross-verified', text)
        self.assertNotIn('two apple endpoints', text)

    def test_manual_live_hit_has_real_fields_without_ping(self):
        payload = monitor.manual_discord_payload([record()])
        self.assertEqual(payload['content'], '')
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertIn('Sherman Oaks', json.dumps(payload))

    def test_automatic_hit_pings_everyone(self):
        payload = monitor.discord_payload([record()])
        self.assertEqual(payload['content'], '@everyone')
        self.assertEqual(payload['allowed_mentions'], {'parse': ['everyone']})

    def test_discord_receipt_is_requested_and_returned(self):
        class Reply(io.BytesIO):
            status = 200
        sent = []
        def post(req, timeout):
            sent.append(req)
            return Reply(b'{"id":"777"}')
        with patch.object(monitor, 'urlopen', side_effect=post):
            receipt = monitor.send_discord(WEBHOOK, monitor.manual_discord_payload([]))
        self.assertEqual(parse_qs(urlsplit(sent[0].full_url).query).get('wait'), ['true'])
        self.assertEqual(receipt, '777')

    def test_manual_check_does_not_modify_notification_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.stock-state'
            path.write_text('do not touch')
            with patch.object(monitor, 'STATE_FILE', path), patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': WEBHOOK, 'TEST_WEBHOOK': 'true'}), patch.object(monitor, 'fetch_inventory', return_value=response()), patch.object(monitor, 'send_discord', return_value='777') as send:
                self.assertEqual(monitor.main(), 0)
            self.assertEqual(path.read_text(), 'do not touch')
            self.assertEqual(send.call_count, 1)

    def test_network_error_preserves_notification_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.stock-state'
            path.write_text('saved state')
            with patch.object(monitor, 'STATE_FILE', path), patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': WEBHOOK, 'TEST_WEBHOOK': 'false'}), patch.object(monitor, 'fetch_inventory', side_effect=RuntimeError('Apple request failed')):
                try:
                    code = monitor.main()
                    self.assertNotEqual(code, 0)
                except RuntimeError:
                    pass
            self.assertEqual(path.read_text(), 'saved state')

    def test_removing_one_store_does_not_reping_for_another(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.stock-state'
            first = [record(), record(SKU2, 'R051')]
            with patch.object(monitor, 'STATE_FILE', path), patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': WEBHOOK, 'TEST_WEBHOOK': 'false'}), patch.object(monitor, 'fetch_inventory', side_effect=[response([(SKU, 'R148'), (SKU2, 'R051')]), response([(SKU, 'R148')])]), patch.object(monitor, 'get_confirmed_inventory', side_effect=[first, first[:1]]), patch.object(monitor, 'send_discord', return_value='777') as send:
                monitor.main()
                monitor.main()
            self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
