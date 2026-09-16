"""Behavior tests for parsing, cross-verification, deduplication, and Discord payloads."""

import unittest

import monitor


class MonitorTests(unittest.TestCase):
    """Verify the small pure functions that keep stock alerts accurate and predictable."""

    def test_extracts_only_available_monitored_products(self):
        """Only explicitly available target SKUs from monitored stores become candidates."""
        payload = {
            "body": {
                "stores": [
                    {
                        "storeNumber": "R148",
                        "storeName": "Apple Sherman Oaks",
                        "partsAvailability": {
                            "MJW64LL/A": {
                                "pickupDisplay": "available",
                                "storePickupQuote": "Today",
                            },
                            "MJWA4LL/A": {
                                "pickupDisplay": "unavailable",
                                "storePickupQuote": "Unavailable",
                            },
                        },
                    }
                ]
            }
        }

        self.assertEqual(
            monitor.extract_available(payload),
            [
                {
                    "sku": "MJW64LL/A",
                    "storage": "256 GB",
                    "store": "Apple Sherman Oaks",
                    "store_id": "R148",
                    "pickup": "Today",
                }
            ],
        )

    def test_extracts_fulfillment_availability_from_captured_shape(self):
        """The fulfillment endpoint should normalize the same SKU/store availability facts."""
        payload = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [
                            {
                                "storeNumber": "R720",
                                "storeName": "Tower Theatre",
                                "partsAvailability": {
                                    "MJWA4LL/A": {
                                        "pickupDisplay": "available",
                                        "messageTypes": {
                                            "regular": {
                                                "storePickupQuote": (
                                                    "Fri Sep 18 at Apple Tower Theatre"
                                                )
                                            }
                                        },
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }

        self.assertEqual(
            monitor.extract_fulfillment_available(payload),
            [
                {
                    "sku": "MJWA4LL/A",
                    "storage": "512 GB",
                    "store": "Tower Theatre",
                    "store_id": "R720",
                    "pickup": "Fri Sep 18 at Apple Tower Theatre",
                }
            ],
        )

    def test_only_candidates_available_on_both_apple_endpoints_are_confirmed(self):
        """A pickup hit must also exist on fulfillment before production may alert."""
        primary = [
            {
                "sku": "MJW64LL/A",
                "storage": "256 GB",
                "store": "Tower Theatre",
                "store_id": "R720",
                "pickup": "Fri Sep 18",
            },
            {
                "sku": "MJWA4LL/A",
                "storage": "512 GB",
                "store": "Sherman Oaks",
                "store_id": "R148",
                "pickup": "Fri Sep 18",
            },
        ]
        fulfillment = {
            "MJW64LL/A": [
                {
                    "sku": "MJW64LL/A",
                    "storage": "256 GB",
                    "store": "Tower Theatre",
                    "store_id": "R720",
                    "pickup": "Fri Sep 18 at Apple Tower Theatre",
                }
            ],
            "MJWA4LL/A": [],
        }

        self.assertEqual(monitor.confirm_available(primary, fulfillment), [primary[0]])

    def test_confirmation_fetches_fulfillment_only_for_skus_with_primary_hits(self):
        """Avoid extra Apple requests when a SKU has no primary pickup candidate."""
        primary_payload = {
            "body": {
                "stores": [
                    {
                        "storeNumber": "R720",
                        "storeName": "Tower Theatre",
                        "partsAvailability": {
                            "MJW64LL/A": {
                                "pickupDisplay": "available",
                                "storePickupQuote": "Fri Sep 18",
                            },
                            "MJWA4LL/A": {"pickupDisplay": "unavailable"},
                        },
                    }
                ]
            }
        }
        fulfillment_payload = {
            "body": {
                "content": {
                    "pickupMessage": {
                        "stores": [
                            {
                                "storeNumber": "R720",
                                "storeName": "Tower Theatre",
                                "partsAvailability": {
                                    "MJW64LL/A": {
                                        "pickupDisplay": "available",
                                        "messageTypes": {
                                            "regular": {
                                                "storePickupQuote": (
                                                    "Fri Sep 18 at Apple Tower Theatre"
                                                )
                                            }
                                        },
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        requested = []

        def fake_fulfillment(sku):
            """Record which SKU verification was requested and return a known response."""
            requested.append(sku)
            return fulfillment_payload

        confirmed = monitor.get_confirmed_inventory(
            primary_payload,
            fulfillment_fetcher=fake_fulfillment,
        )

        self.assertEqual(requested, ["MJW64LL/A"])
        self.assertEqual(
            [(item["sku"], item["store_id"]) for item in confirmed],
            [("MJW64LL/A", "R720")],
        )

    def test_fingerprint_is_stable_regardless_of_result_order(self):
        """Apple reordering stores must not generate duplicate stock alerts."""
        results = [
            {"sku": "MJW64LL/A", "store_id": "R148"},
            {"sku": "MJWA4LL/A", "store_id": "R051"},
        ]
        self.assertEqual(
            monitor.fingerprint(results),
            monitor.fingerprint(list(reversed(results))),
        )

    def test_manual_payload_reports_confirmed_inventory_without_ping(self):
        """Manual live checks should show real confirmed stock without notifying everyone."""
        payload = monitor.manual_discord_payload(
            [
                {
                    "sku": "MJW64LL/A",
                    "storage": "256 GB",
                    "store": "Apple Sherman Oaks",
                    "store_id": "R148",
                    "pickup": "Today",
                }
            ]
        )

        self.assertEqual(payload["content"], "")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("Manual Live Check", payload["embeds"][0]["title"])
        self.assertIn("256 GB", payload["embeds"][0]["fields"][0]["value"])
        self.assertIn("Sherman Oaks", payload["embeds"][0]["fields"][0]["value"])

    def test_automatic_payload_pings_everyone_and_says_cross_verified(self):
        """Automatic confirmed stock alerts should ping everyone and identify verification."""
        payload = monitor.discord_payload(
            [
                {
                    "sku": "MJW64LL/A",
                    "storage": "256 GB",
                    "store": "Apple Sherman Oaks",
                    "store_id": "R148",
                    "pickup": "Today",
                }
            ]
        )

        self.assertEqual(payload["content"], "@everyone")
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})
        self.assertIn("confirmed by two", payload["embeds"][0]["description"])


if __name__ == "__main__":
    unittest.main()
