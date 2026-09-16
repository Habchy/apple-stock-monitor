#!/usr/bin/env python3
"""Tiny Apple pickup monitor that alerts Discord when a Burgundy iPhone appears."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# The only two product configurations this monitor should ever alert for.
PRODUCTS = {"MJW64LL/A": "256 GB", "MJWA4LL/A": "512 GB"}

# Exact LA-area Apple Store IDs selected for this monitor. Results from any
# other stores returned by Apple's location search are deliberately ignored.
STORES = {"R148", "R108", "R124", "R050", "R051", "R189", "R023", "R001", "R451", "R720"}

# GitHub Actions caches this tiny fingerprint between runs. An empty file means
# no monitored inventory was available on the previous check.
STATE_FILE = Path(".stock-state")
APPLE_ENDPOINT = "https://www.apple.com/shop/retail/pickup-message"


def fetch_inventory() -> dict:
    """Fetch both target SKUs around Los Angeles in one lightweight Apple request."""
    params = [("location", "90077"), ("parts.0", "MJW64LL/A"), ("parts.1", "MJWA4LL/A")]
    request = Request(
        f"{APPLE_ENDPOINT}?{urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def extract_available(payload: dict) -> list[dict[str, str]]:
    """Normalize only available target products at the ten explicitly monitored stores."""
    results = []
    for store in payload.get("body", {}).get("stores", []):
        store_id = store.get("storeNumber", "")
        if store_id not in STORES:
            continue

        for sku, availability in store.get("partsAvailability", {}).items():
            if sku in PRODUCTS and availability.get("pickupDisplay") == "available":
                results.append({
                    "sku": sku,
                    "storage": PRODUCTS[sku],
                    "store": store.get("storeName", store_id),
                    "store_id": store_id,
                    "pickup": availability.get("storePickupQuote", "Available for pickup"),
                })
    return results


def fingerprint(results: list[dict[str, str]]) -> str:
    """Create a stable state ID from SKU/store pairs so identical stock is not re-alerted."""
    pairs = sorted(f"{item['sku']}:{item['store_id']}" for item in results)
    return hashlib.sha256("|".join(pairs).encode()).hexdigest() if pairs else ""


def _stock_fields(results: list[dict[str, str]]) -> list[dict[str, object]]:
    """Format normalized Apple availability as reusable Discord embed fields."""
    return [{
        "name": f"📱 {item['storage']} Burgundy",
        "value": f"**{item['storage']} • {item['store']}**\n{item['pickup']}",
        "inline": False,
    } for item in results]


def discord_payload(results: list[dict[str, str]]) -> dict:
    """Build the real automatic stock alert and explicitly permit the @everyone mention."""
    return {
        "content": "@everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": "🚨 iPhone 18 Pro Max Burgundy In Stock",
            "description": "Apple pickup inventory just became available.",
            "color": 0x7A263A,
            "fields": _stock_fields(results),
            "footer": {"text": "Apple LA Stock Monitor • Check Apple immediately before driving"},
        }],
    }


def manual_discord_payload(results: list[dict[str, str]]) -> dict:
    """Report a real Apple API check to Discord without generating an @everyone notification."""
    if results:
        description = "Live Apple inventory was queried successfully. Current monitored availability:"
        fields = _stock_fields(results)
        color = 0x7A263A
    else:
        description = "Live Apple inventory was queried successfully. **No Burgundy stock is currently available** at the monitored stores."
        fields = []
        color = 0x5865F2

    return {
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "🔎 Apple Stock Monitor • Manual Live Check",
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": "Real-time Apple API result • Manual check • No @everyone ping"},
        }],
    }


def send_discord(webhook: str, payload: dict) -> None:
    """POST one JSON alert to Discord and fail loudly if Discord rejects it."""
    body = json.dumps(payload).encode()
    request = Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "apple-stock-monitor"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def main() -> int:
    """Check inventory once, optionally report it, and alert only on a new automatic stock state."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("DISCORD_WEBHOOK_URL is required", file=sys.stderr)
        return 2

    # Both manual and automatic modes query the exact same live Apple endpoint and parser.
    available = extract_available(fetch_inventory())

    # Manual mode always reports the current live result, even when nothing is available.
    # It deliberately does not alter deduplication state or ping @everyone.
    if os.environ.get("TEST_WEBHOOK", "").lower() == "true":
        send_discord(webhook, manual_discord_payload(available))
        print(f"Sent manual live inventory check with {len(available)} available option(s).")
        return 0

    current = fingerprint(available)
    previous = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""

    if current and current != previous:
        send_discord(webhook, discord_payload(available))
        print(f"Alerted for {len(available)} available option(s).")
    else:
        print("No new availability.")

    # Persist even an empty state so a later restock can alert again after stock disappears.
    STATE_FILE.write_text(current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
