import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import load_config, queries
from .campaign import estimate, tick
from .http import BlockedError, FetchError
from .normalize import SchemaError, normalize
from .pelikan import PelikanClient, search_url
from .storage import CapacityError, SupabaseStore, save_outbox


def now():
    return datetime.now(timezone.utc).isoformat()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pelikan → Rome → Supabase; 1 adult, one-way, economy')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--outbox', default='data/outbox')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'run'):
        command = sub.add_parser(name)
        command.add_argument('--origin')
        command.add_argument('--date')
        if name == 'run':
            command.add_argument('--local-only', action='store_true', help='Save outbox without Supabase')
            command.add_argument('--until', help=argparse.SUPPRESS)
    campaign = sub.add_parser('tick', help='Run if due within a persistent 21-day window')
    campaign.add_argument('--state', default='data/campaign.json')
    campaign.add_argument('--local-only', action='store_true')
    estimation = sub.add_parser('estimate', help='Offline three-week storage estimate')
    estimation.add_argument('--offers-per-search', type=int, default=20)
    sub.add_parser('usage', help='Read actual DB and flight-table sizes from Supabase')
    sub.add_parser('sync', help='Retry pending outbox uploads; makes no Pelikan requests')
    args = parser.parse_args(argv)
    try:
        if args.command == 'usage':
            print(json.dumps(SupabaseStore().usage(), indent=2))
            return 0
        if args.command == 'sync':
            store = SupabaseStore()
            for path in sorted(Path(args.outbox).glob('*.json')):
                store.upload(path)
                print('Uploaded', path.name)
            return 0
        config = load_config(args.config)
        if args.command == 'estimate':
            if args.offers_per_search < 0:
                raise ValueError('Offer count must be nonnegative')
            print(json.dumps(estimate(config, len(list(queries(config))), args.offers_per_search), indent=2))
            return 0
        if args.command == 'tick':
            if not args.local_only:
                SupabaseStore()  # Validate settings before claiming the first slot.
            return tick(args.state, config, lambda end: main([
                '--config', args.config, '--outbox', args.outbox, 'run', '--until', end,
                *(['--local-only'] if args.local_only else [])]))
        jobs = list(queries(config, args.origin, args.date))
        if args.command == 'plan':
            print(json.dumps({'searches': len(jobs), 'event': config['event_start'],
                              'buffer_minutes': config['arrival_buffer_minutes'],
                              'urls': [search_url(a['iata'], d) for a, d in jobs]}, indent=2))
            return 0
        store = None if args.local_only else SupabaseStore()
        client = PelikanClient(config)
        until = datetime.fromisoformat(args.until) if args.until else None
        run_id, failures = str(uuid4()), 0
        for airport, day in jobs:
            if until and datetime.now(timezone.utc) >= until:
                print('Collection deadline reached')
                break
            started = now()
            url = search_url(airport['iata'], day)
            search = {'id': str(uuid4()), 'run_id': run_id, 'event_id': config['event_id'],
                      'origin': airport['iata'], 'departure_date': day.isoformat(),
                      'started_at': started, 'event_start': config['event_start'],
                      'arrival_buffer_minutes': config['arrival_buffer_minutes'],
                      'search_url': url, 'status': 'error', 'error': None}
            offers, pages, excluded, stop = [], [], {}, False
            print(f"Searching {airport['iata']} → ROM {day}", flush=True)
            try:
                pages = client.search(airport['iata'], day)
                offers, excluded = normalize(pages, airport, day, config, url)
                search['status'] = 'ok' if offers else 'no_matching_offers'
            except BlockedError as exc:
                search['status'], search['error'], stop = 'blocked', str(exc), True
                failures += 1
            except (FetchError, SchemaError, ValueError) as exc:
                search['error'] = str(exc)
                failures += 1
            search.update(finished_at=now(), observed_at=now(), offer_count=len(offers),
                          eligible_count=sum(o['eligible_for_event'] and o['available'] for o in offers),
                          diagnostics={'pages': len(pages), 'excluded': excluded,
                                       'source_total': pages[0].get('total') if pages else None})
            snapshot = {'schema_version': 1, 'search': search, 'offers': offers}
            path = save_outbox(args.outbox, snapshot)
            if store:
                try:
                    store.upload(path)
                except CapacityError as exc:
                    print(f'Supabase: {exc}. Continuing collection to local outbox.', file=sys.stderr)
                    failures += 1
                    store = None
                except FetchError as exc:
                    print(f'Supabase: {exc}. Continuing locally; run sync after recovery.', file=sys.stderr)
                    failures += 1
                    store = None
            print(f"  {search['status']}: {len(offers)} offers; {search['eligible_count']} eligible", flush=True)
            if search['error']:
                print('  ' + search['error'], file=sys.stderr)
            if stop:
                break
        return 1 if failures else 0
    except (FetchError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
