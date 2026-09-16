"""Behavior tests for the stock monitor's parsing, deduplication, and Discord payload."""

import unittest

import monitor


class MonitorTests(unittest.TestCase):
    """Verify the small pure functions that make alerting safe and predictable."""

    def test_extracts_only_available_monitored_products(self):
        """Only explicitly available Burgundy SKUs should become alerts."""
        payload = {
            "body": {"stores": [{
                "storeNumber": "R148",
                "storeName": "Apple Sherman Oaks",
                "partsAvailability": {
                    "MJW64LL/A": {"pickupDisplay": "available", "storePickupQuote": "Today"},
                    "MJWA4LL/A": {"pickupDisplay": "unavailable", "storePickupQuote": "Unavailable"},
                },
            }]}
        }
        found = monitor.extract_available(payload)
        self.assertEqual(found, [{
            "sku": "MJW64LL/A",
            "storage": "256 GB",
            "store": "Apple Sherman Oaks",
            "store_id": "R148",
            "pickup": "Today",
        }])

    def test_fingerprint_is_stable_regardless_of_result_order(self):
        """The same inventory state must not alert twice just because Apple reordered stores."""
        a = [
            {"sku": "MJW64LL/A", "store_id": "R148"},
            {"sku": "MJWA4LL/A", "store_id": "R051"},
        ]
        self.assertEqual(monitor.fingerprint(a), monitor.fingerprint(list(reversed(a))))

    def test_test_payload_is_clearly_marked_and_does_not_claim_real_stock(self):
        """Manual webhook tests must prove delivery without pretending inventory exists."""
        payload = monitor.test_discord_payload()
        self.assertEqual(payload["content"], "@everyone")
        self.assertIn("Test Successful", payload["embeds"][0]["title"])
        self.assertIn("does not indicate stock", payload["embeds"][0]["description"])

    def test_discord_payload_pings_everyone_and_allows_the_mention(self):
        """A stock alert must generate a real @everyone ping and a rich embed."""
        payload = monitor.discord_payload([{
            "sku": "MJW64LL/A",
            "storage": "256 GB",
            "store": "Apple Sherman Oaks",
            "store_id": "R148",
            "pickup": "Today",
        }])
        self.assertEqual(payload["content"], "@everyone")
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})
        self.assertIn("iPhone 18 Pro Max", payload["embeds"][0]["title"])
        self.assertIn("256 GB", payload["embeds"][0]["fields"][0]["value"])


if __name__ == "__main__":
    unittest.main()
