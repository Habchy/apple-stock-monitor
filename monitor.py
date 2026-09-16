#!/usr/bin/env python3
"""Check Apple pickup inventory once; notify Discord only for newly confirmed stock."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

# This explicit allowlist prevents recommendation results for other phones from alerting.
PRODUCTS = {'MJW64LL/A': '256 GB', 'MJWA4LL/A': '512 GB'}
COLOR = 'Burgundy'
STORES = {
    'R148': 'Sherman Oaks', 'R108': 'Century City', 'R124': 'Beverly Center',
    'R050': 'The Grove', 'R051': 'Third Street Promenade', 'R189': 'Topanga',
    'R023': 'Northridge', 'R001': 'Glendale Galleria',
    'R451': 'The Americana at Brand', 'R720': 'Tower Theatre',
}
LOCATION = '90077'
STATE_FILE = Path('.stock-state')
PICKUP_ENDPOINT = 'https://www.apple.com/shop/retail/pickup-message'
FULFILLMENT_ENDPOINT = 'https://www.apple.com/shop/fulfillment-messages'


def _fetch_json(url: str) -> dict:
    """Read public Apple JSON with bounded GET retries; never bypass access restrictions."""
    for attempt in range(2):
        try:
            request = Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
                'Cache-Control': 'no-cache',
            })
            with urlopen(request, timeout=12) as reply:
                data = json.load(reply)
                print(f'Apple {urlsplit(url).path}: HTTP {reply.status}')
            if not isinstance(data, dict):
                raise RuntimeError('Apple returned a non-object JSON response')
            return data
        except HTTPError as error:
            # A blocked endpoint (including 541) is not hammered with retries.
            if attempt == 0 and error.code in (502, 503, 504):
                time.sleep(1)
                continue
            raise RuntimeError(f'Apple request failed: HTTP {error.code}') from None
        except (URLError, TimeoutError, OSError):
            if attempt == 0:
                time.sleep(1)
                continue
            raise RuntimeError('Apple request failed: connection or timeout') from None
        except (ValueError, UnicodeError):
            raise RuntimeError('Apple returned invalid JSON, not inventory') from None
    raise RuntimeError('Apple request did not complete')


def _stores(payload: dict, fulfillment: bool = False) -> list[dict]:
    """Validate the observed response wrapper; a missing list is never treated as no stock."""
    try:
        if str(payload['head']['status']) != '200':
            raise ValueError
        body = payload['body']
        rows = body['content']['pickupMessage']['stores'] if fulfillment else body['stores']
        if not isinstance(rows, list) or not rows:
            raise ValueError
        if any(not isinstance(row, dict) or not isinstance(row.get('storeNumber'), str) for row in rows):
            raise ValueError
        return rows
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('Apple response is incomplete or its inventory schema changed') from None


def _observations(payload: dict, fulfillment: bool = False) -> dict[tuple[str, str], dict]:
    """Keep explicit statuses and validate product identity before interpreting a stock cell."""
    records = {}
    for store in _stores(payload, fulfillment):
        store_id = store['storeNumber']
        if store_id not in STORES:
            continue
        parts = store.get('partsAvailability')
        if not isinstance(parts, dict):
            raise RuntimeError(f'Apple omitted inventory for {store_id}')
        for sku, size in PRODUCTS.items():
            if sku not in parts:
                continue  # Missing cells trigger a targeted request, not an unavailable result.
            info = parts[sku]
            try:
                status = info['pickupDisplay']
                regular = info['messageTypes']['regular']
                title = regular['storePickupProductTitle']
                expected = f'iPhone 18 Pro Max {size} {COLOR}'
                if ''.join(title.split()).casefold() != ''.join(expected.split()).casefold():
                    raise ValueError
                if info.get('partNumber', sku) != sku or status not in ('available', 'unavailable'):
                    raise ValueError
                quote = regular.get('storePickupQuote') or info.get('storePickupQuote') or info.get('pickupSearchQuote')
                if status == 'available' and not isinstance(quote, str):
                    raise ValueError
            except (KeyError, TypeError, AttributeError, ValueError):
                raise RuntimeError(f'Apple returned an unrecognized product/status for {sku} at {store_id}') from None
            records[(sku, store_id)] = {
                'sku': sku, 'storage': size, 'store': STORES[store_id],
                'store_id': store_id, 'pickup': quote or 'Currently unavailable', 'status': status,
            }
    return records


def _pickup_url(store: str | None = None) -> str:
    """Batch both SKUs and keep the same Bel Air search origin for all requests."""
    params = [('location', LOCATION)] + [(f'parts.{i}', sku) for i, sku in enumerate(PRODUCTS)]
    if store:
        params.append(('store', store))
    return f'{PICKUP_ENDPOINT}?{urlencode(params)}'


def fetch_inventory() -> dict:
    """Usually make one GET; query missing stores directly until all 20 cells are accounted for."""
    data = _fetch_json(_pickup_url())
    rows = {row['storeNumber']: row for row in _stores(data)}
    observed = _observations(data)
    missing_stores = [store for store in STORES if any((sku, store) not in observed for sku in PRODUCTS)]
    for store in missing_stores:
        direct = _fetch_json(_pickup_url(store))
        fresh = _observations(direct)
        if any((sku, store) not in fresh for sku in PRODUCTS):
            raise RuntimeError(f'Apple did not return both target SKUs for {store}; coverage incomplete')
        # Only replace the requested store, not other possibly reordered regional results.
        rows[store] = next(row for row in _stores(direct) if row['storeNumber'] == store)
    return {'head': {'status': '200'}, 'body': {'stores': list(rows.values())}}


def fetch_fulfillment(sku: str) -> dict:
    """Try the second storefront endpoint only for a SKU with a primary candidate."""
    params = {'fae': 'true', 'pl': 'true', 'mts.0': 'regular', 'cppart': 'UNLOCKED/US',
              'parts.0': sku, 'location': LOCATION}
    return _fetch_json(f'{FULFILLMENT_ENDPOINT}?{urlencode(params)}')


def extract_available(payload: dict) -> list[dict]:
    """Expose only positively available allowlisted pairs from a validated primary response."""
    return [row for row in _observations(payload).values() if row['status'] == 'available']


def extract_fulfillment_available(payload: dict) -> list[dict]:
    """Use the nested fulfillment quote rather than a generic pickup placeholder."""
    return [row for row in _observations(payload, True).values() if row['status'] == 'available']


def get_confirmed_inventory(primary_payload: dict, fulfillment_fetcher=None) -> list[dict]:
    """Confirm each candidate; if fulfillment is blocked/missing, require a fresh direct-store hit.

    Both endpoints belong to Apple and are not independent sources. An explicit
    fulfillment 'unavailable' is respected. Only an error or missing store uses
    the fallback, whose real verification method is attached to the alert.
    """
    primary = extract_available(primary_payload)
    if not primary:
        print('Primary candidates: 0; no confirmation requests needed.')
        return []
    fetcher = fulfillment_fetcher or fetch_fulfillment
    confirmations, direct_cache = {}, {}
    for sku in sorted({row['sku'] for row in primary}):
        try:
            confirmations[sku] = _observations(fetcher(sku), True)
        except RuntimeError as error:
            print(f'Fulfillment unavailable for {sku}: {error}; using direct-store confirmation.')
            confirmations[sku] = {}
    confirmed = []
    for candidate in primary:
        key = candidate['sku'], candidate['store_id']
        check = confirmations[key[0]].get(key)
        method = 'Fulfillment matched'
        if check is None:
            # One store recheck can confirm both sizes if both have candidate stock.
            if key[1] not in direct_cache:
                direct_cache[key[1]] = _observations(_fetch_json(_pickup_url(key[1])))
            check = direct_cache[key[1]].get(key)
            if check is None:
                raise RuntimeError(f'Unable to confirm inventory for {key[0]} at {key[1]}')
            method = 'Direct store recheck (fulfillment unavailable or incomplete)'
        if check['status'] == 'available':
            confirmed.append({**check, 'verification': method})
        else:
            print(f'Not confirmed: {key[0]} at {key[1]}; no stock alert for this pair.')
    return confirmed


def _stock_fields(results: list[dict]) -> list[dict]:
    """Share the real alert layout with manual reports, within Discord embed size limits."""
    return [{'name': f"📱 {row['storage']} {COLOR}",
             'value': f"**{row['store']}**\n{row['pickup'][:180]}\n*{row.get('verification', 'Live check')}*",
             'inline': False} for row in results]


def _message(results: list[dict], manual: bool, candidate_count: int = 0) -> dict:
    """Build a Burgundy-themed live report without pretending skipped checks happened."""
    if results:
        description = 'Pickup availability rechecked. Confirm your reservation with Apple before travelling.'
    elif candidate_count:
        description = f'{candidate_count} primary candidate(s), but none passed confirmation. No stock is confirmed.'
    else:
        description = '**No Burgundy stock currently available.** Primary inventory checked; no candidates to recheck.'
    return {
        'content': '' if manual else '@everyone',
        'allowed_mentions': {'parse': [] if manual else ['everyone']},
        'embeds': [{
            'title': '🔎 Apple Stock Monitor • Manual Live Check' if manual else '🍷 iPhone 18 Pro Max • Pickup available',
            'description': description, 'color': 0x7A263A, 'fields': _stock_fields(results),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'footer': {'text': f'{len(STORES)} stores • {len(PRODUCTS)} configurations • ' + ('Manual report, no ping' if manual else 'New confirmed availability')},
        }],
    }


def discord_payload(results: list[dict]) -> dict:
    """Automatic alerts mention everyone only after a new pair passes confirmation."""
    return _message(results, False)


def manual_discord_payload(results: list[dict], candidate_count: int = 0) -> dict:
    """Manual live reports always show the actual result and never permit mentions."""
    return _message(results, True, candidate_count)


def send_discord(webhook: str, payload: dict) -> str:
    """Request a Discord message receipt; never print a secret URL or retry an ambiguous POST."""
    url = urlsplit(webhook)
    if url.scheme != 'https' or url.netloc not in ('discord.com', 'canary.discord.com', 'ptb.discord.com') or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[\w-]+', url.path):
        raise RuntimeError('DISCORD_WEBHOOK_URL is not a supported Discord webhook URL')
    query = [(k, v) for k, v in parse_qsl(url.query) if k != 'wait'] + [('wait', 'true')]
    target = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))
    request = Request(target, data=json.dumps(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/json', 'User-Agent': 'apple-stock-monitor'})
    try:
        with urlopen(request, timeout=15) as reply:
            data = json.load(reply)
        if not isinstance(data, dict) or not isinstance(data.get('id'), str):
            raise RuntimeError('Discord did not return a message receipt')
        return data['id']
    except HTTPError as error:
        raise RuntimeError(f'Discord rejected the message: HTTP {error.code}') from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError('Discord delivery could not be confirmed; notification state was not saved') from None


def _load_announced() -> set[str]:
    """Load per-pair deduplication; migrate the old one-way hash once without guessing its pairs."""
    if not STATE_FILE.exists():
        return set()
    raw = STATE_FILE.read_text().strip()
    if not raw or re.fullmatch(r'[0-9a-f]{64}', raw):
        if raw:
            print('Migrating legacy fingerprint; current stock may be announced once again.')
        return set()
    try:
        data = json.loads(raw)
        if data['version'] != 2 or not isinstance(data['announced'], list) or any(not isinstance(v, str) for v in data['announced']):
            raise ValueError
        return set(data['announced'])
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Notification state is corrupt; refusing to reset it silently') from None


def _pair(row: dict) -> str:
    """Stable identity deliberately ignores changing quote text to avoid repeated pings."""
    return f"{row['sku']}:{row['store_id']}"


def _summary(text: str) -> None:
    """Show evidence in both the job log and GitHub's human-readable run summary."""
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(text + '\n')


def main() -> int:
    """Run once; API failures cannot become no-stock results or erase notification history."""
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not webhook:
        print('DISCORD_WEBHOOK_URL is required', file=sys.stderr)
        return 2
    manual = os.environ.get('TEST_WEBHOOK', '').lower() == 'true'
    try:
        primary = fetch_inventory()
        observations = _observations(primary)
        expected = {(sku, store) for sku in PRODUCTS for store in STORES}
        if not expected.issubset(observations):
            raise RuntimeError('Primary inventory coverage incomplete; this is not a no-stock result')
        candidates = extract_available(primary)
        confirmed = get_confirmed_inventory(primary)
        lines = ['### Apple inventory check', f'Checked {len(expected)} SKU/store pairs across {len(STORES)} stores.',
                 f'Primary candidates: {len(candidates)}. Confirmed options: {len(confirmed)}.', '',
                 '| Store | ' + ' | '.join(PRODUCTS.values()) + ' |',
                 '| :--- | ' + ' | '.join(':---' for _ in PRODUCTS) + ' |']
        for store, name in STORES.items():
            lines.append('| ' + name + ' | ' + ' | '.join(observations[(sku, store)]['status'] for sku in PRODUCTS) + ' |')
        _summary('\n'.join(lines))
        if manual:
            receipt = send_discord(webhook, manual_discord_payload(confirmed, len(candidates)))
            _summary(f'Manual live report delivered. Discord message ID: {receipt}. No mentions; state unchanged.')
            return 0
        previous = _load_announced()
        current = {_pair(row) for row in confirmed}
        if current - previous:
            receipt = send_discord(webhook, discord_payload(confirmed))
            _summary(f'New confirmed stock delivered. Discord message ID: {receipt}.')
        else:
            _summary('No new confirmed pairs. No Discord alert sent.')
        # Preserve an announced pair while the primary still sees it, even during a
        # confirmation disagreement. Remove it only after explicit primary unavailability.
        next_state = (previous & {_pair(row) for row in candidates}) | current
        temporary = STATE_FILE.with_name(STATE_FILE.name + '.tmp')
        temporary.write_text(json.dumps({'version': 2, 'announced': sorted(next_state)}))
        temporary.replace(STATE_FILE)
        return 0
    except (RuntimeError, OSError) as error:
        _summary(f'CHECK FAILED: {error}. No no-stock conclusion; notification state unchanged.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
