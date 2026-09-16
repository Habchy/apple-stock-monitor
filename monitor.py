#!/usr/bin/env python3
"""Tiny Apple pickup monitor for Burgundy iPhone 18 Pro Max inventory."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# These are the only two configurations that can ever trigger an alert.
# Keeping this map explicit makes accidental alerts for other colors or sizes impossible.
PRODUCTS = {
    "MJW64LL/A": "256 GB",
    "MJWA4LL/A": "512 GB",
}

# Only these ten LA-area Apple Stores are considered valid monitor targets.
# Apple may return additional nearby stores, but they are deliberately ignored.
STORES = {
    "R148",  # Sherman Oaks
    "R108",  # Century City
    "R124",  # Beverly Center
    "R050",  # The Grove
    "R051",  # Third Street Promenade
    "R189",  # Topanga
    "R023",  # Northridge
    "R001",  # Glendale Galleria
    "R451",  # The Americana at Brand
    "R720",  # Tower Theatre
}

# GitHub Actions caches this one-line fingerprint between runs. An empty file means
# no confirmed monitored inventory was available on the previous check.
STATE_FILE = Path(".stock-state")

# Apple exposes the same inventory through two storefront endpoints. The primary
# endpoint is cheap and can check both SKUs at once. The fulfillment endpoint is
# used only as a second independent confirmation when the primary reports a hit.
PICKUP_ENDPOINT = "https://www.apple.com/shop/retail/pickup-message"
FULFILLMENT_ENDPOINT = "https://www.apple.com/shop/fulfillment-messages"


def _fetch_json(url: str) -> dict:
    """Fetch one Apple storefront JSON response with a browser-like user agent."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def fetch_inventory() -> dict:
    """Query Apple's primary pickup endpoint for both Burgundy SKUs in one request."""
    params = [
        ("location", "90077"),
        ("parts.0", "MJW64LL/A"),
        ("parts.1", "MJWA4LL/A"),
    ]
    return _fetch_json(f"{PICKUP_ENDPOINT}?{urlencode(params)}")


def fetch_fulfillment(sku: str) -> dict:
    """Query Apple's fulfillment endpoint for one exact Burgundy SKU.

    This request mirrors the parameters Apple's own storefront uses. It is only
    performed for a SKU after the primary pickup endpoint reports at least one
    monitored store as available, so ordinary no-stock runs stay lightweight.
    """
    params = [
        ("fae", "true"),
        ("pl", "true"),
        ("mts.0", "regular"),
        ("cppart", "UNLOCKED/US"),
        ("parts.0", sku),
        ("location", "Los Angeles, CA"),
    ]
    return _fetch_json(f"{FULFILLMENT_ENDPOINT}?{urlencode(params)}")


def _normalize_store_hit(
    store: dict,
    sku: str,
    availability: dict,
    pickup_quote: str,
) -> dict[str, str]:
    """Convert one Apple store/SKU availability object into our compact internal shape."""
    store_id = store.get("storeNumber", "")
    return {
        "sku": sku,
        "storage": PRODUCTS[sku],
        "store": store.get("storeName", store_id),
        "store_id": store_id,
        "pickup": pickup_quote,
    }


def extract_available(payload: dict) -> list[dict[str, str]]:
    """Parse available target SKU/store pairs from Apple's primary pickup response."""
    results = []

    for store in payload.get("body", {}).get("stores", []):
        store_id = store.get("storeNumber", "")
        if store_id not in STORES:
            continue

        for sku, availability in store.get("partsAvailability", {}).items():
            if sku not in PRODUCTS or availability.get("pickupDisplay") != "available":
                continue

            results.append(
                _normalize_store_hit(
                    store,
                    sku,
                    availability,
                    availability.get("storePickupQuote", "Available for pickup"),
                )
            )

    return results


def extract_fulfillment_available(payload: dict) -> list[dict[str, str]]:
    """Parse available target SKU/store pairs from Apple's fulfillment response."""
    results = []
    stores = (
        payload.get("body", {})
        .get("content", {})
        .get("pickupMessage", {})
        .get("stores", [])
    )

    for store in stores:
        store_id = store.get("storeNumber", "")
        if store_id not in STORES:
            continue

        for sku, availability in store.get("partsAvailability", {}).items():
            if sku not in PRODUCTS or availability.get("pickupDisplay") != "available":
                continue

            # In the fulfillment response, Apple's detailed pickup timing is nested
            # under messageTypes.regular. Fall back to pickupSearchQuote if needed.
            regular = availability.get("messageTypes", {}).get("regular", {})
            pickup_quote = regular.get(
                "storePickupQuote",
                availability.get("pickupSearchQuote", "Available for pickup"),
            )
            results.append(_normalize_store_hit(store, sku, availability, pickup_quote))

    return results


def confirm_available(
    primary: list[dict[str, str]],
    fulfillment_by_sku: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    """Keep only SKU/store pairs independently marked available by both Apple endpoints."""
    confirmed_pairs = {
        (item["sku"], item["store_id"])
        for sku_results in fulfillment_by_sku.values()
        for item in sku_results
    }

    # Preserve the primary endpoint's ordering and pickup quote for the Discord alert.
    return [
        item
        for item in primary
        if (item["sku"], item["store_id"]) in confirmed_pairs
    ]


def get_confirmed_inventory(
    primary_payload: dict,
    fulfillment_fetcher=fetch_fulfillment,
) -> list[dict[str, str]]:
    """Cross-check primary pickup candidates against Apple's fulfillment endpoint.

    A normal no-stock run makes exactly one Apple request. A second request is made
    only for a SKU that has a primary hit. This gives us stronger confirmation while
    avoiding unnecessary traffic and keeping scheduled runs fast.
    """
    primary = extract_available(primary_payload)
    if not primary:
        return []

    candidate_skus = sorted({item["sku"] for item in primary})
    fulfillment_by_sku = {
        sku: extract_fulfillment_available(fulfillment_fetcher(sku))
        for sku in candidate_skus
    }
    return confirm_available(primary, fulfillment_by_sku)


def fingerprint(results: list[dict[str, str]]) -> str:
    """Create a stable state ID so identical confirmed inventory is not re-alerted."""
    pairs = sorted(f"{item['sku']}:{item['store_id']}" for item in results)
    return hashlib.sha256("|".join(pairs).encode()).hexdigest() if pairs else ""


def _stock_fields(results: list[dict[str, str]]) -> list[dict[str, object]]:
    """Format normalized availability records as reusable Discord embed fields."""
    return [
        {
            "name": f"📱 {item['storage']} Burgundy",
            "value": f"**{item['storage']} • {item['store']}**\n{item['pickup']}",
            "inline": False,
        }
        for item in results
    ]


def discord_payload(results: list[dict[str, str]]) -> dict:
    """Build the automatic alert and explicitly allow the requested @everyone mention."""
    return {
        "content": "@everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [
            {
                "title": "🚨 iPhone 18 Pro Max Burgundy In Stock",
                "description": "Apple pickup inventory was confirmed by two live Apple endpoints.",
                "color": 0x7A263A,
                "fields": _stock_fields(results),
                "footer": {
                    "text": "Apple LA Stock Monitor • Cross-verified • Check Apple immediately"
                },
            }
        ],
    }


def manual_discord_payload(results: list[dict[str, str]]) -> dict:
    """Report a live cross-verified manual check without notifying @everyone."""
    if results:
        description = (
            "Live Apple inventory was queried and cross-verified. "
            "Current confirmed monitored availability:"
        )
        fields = _stock_fields(results)
        color = 0x7A263A
    else:
        description = (
            "Live Apple inventory was queried and cross-verified. "
            "**No Burgundy stock is currently confirmed** at the monitored stores."
        )
        fields = []
        color = 0x5865F2

    return {
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "🔎 Apple Stock Monitor • Manual Live Check",
                "description": description,
                "color": color,
                "fields": fields,
                "footer": {
                    "text": "Two Apple endpoints • Manual check • No @everyone ping"
                },
            }
        ],
    }


def send_discord(webhook: str, payload: dict) -> None:
    """POST one JSON message to Discord and fail loudly if Discord rejects it."""
    body = json.dumps(payload).encode()
    request = Request(
        webhook,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "apple-stock-monitor",
        },
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def main() -> int:
    """Check Apple, cross-verify hits, and alert only on new confirmed inventory."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("DISCORD_WEBHOOK_URL is required", file=sys.stderr)
        return 2

    # Every run begins with one primary pickup request. Fulfillment verification is
    # automatically skipped when the primary endpoint reports no candidate stock.
    confirmed = get_confirmed_inventory(fetch_inventory())

    # Manual mode always sends the current verified result to Discord, including an
    # explicit no-stock result. It never changes deduplication state or pings everyone.
    if os.environ.get("TEST_WEBHOOK", "").lower() == "true":
        send_discord(webhook, manual_discord_payload(confirmed))
        print(
            f"Sent manual live inventory check with {len(confirmed)} "
            "cross-verified option(s)."
        )
        return 0

    current = fingerprint(confirmed)
    previous = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""

    if current and current != previous:
        send_discord(webhook, discord_payload(confirmed))
        print(f"Alerted for {len(confirmed)} cross-verified available option(s).")
    else:
        print("No new cross-verified availability.")

    # Persist even an empty state. If stock later disappears and then comes back,
    # the new restock will produce a different transition and can alert again.
    STATE_FILE.write_text(current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
