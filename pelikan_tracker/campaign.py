"""Persistent fixed-window collection. Invoke tick periodically from cron (Linux)."""
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def write_state(path, state):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def tick(path, config, collect, current=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Collection already running')
            return 0
        current = current or datetime.now(timezone.utc)
        if path.exists():
            state = json.loads(path.read_text())
            if state['event_id'] != config['event_id']:
                raise ValueError('Campaign state belongs to another event')
        else:
            state = {'event_id': config['event_id'], 'started_at': current.isoformat(),
                     'ends_at': (current + timedelta(days=config['collection_days'])).isoformat(),
                     'interval_hours': config['collection_interval_hours'], 'last_slot': -1}
        end = datetime.fromisoformat(state['ends_at'])
        if current >= end:
            print('Collection window finished; existing data retained')
            return 0
        slot = int((current - datetime.fromisoformat(state['started_at'])).total_seconds()
                   // (state['interval_hours'] * 3600))
        if slot <= state['last_slot']:
            print('No collection due')
            return 0
        # Claim before work to prevent duplicate scans after a crash. Missed slots are not backfilled.
        state['last_slot'] = slot
        state['last_status'] = 'running'
        write_state(path, state)
        result = collect(state['ends_at'])
        state['last_status'] = 'ok' if result == 0 else 'errors'
        write_state(path, state)
        return result


def estimate(config, searches, offers_per_search):
    import math
    runs = math.ceil(config['collection_days'] * 24 / config['collection_interval_hours'])
    checks = runs * searches
    rows = checks * offers_per_search
    # Planning assumptions, NOT a measurement of PostgreSQL storage.
    return {'days': config['collection_days'], 'runs': runs, 'searches': checks,
            'offers_per_search_assumption': offers_per_search, 'observation_rows': rows,
            'estimated_flight_mb': round((rows * 2000 + checks * 2000) / 1_000_000, 2),
            'assumed_bytes_per_observation_including_indexes': 2000,
            'flight_budget_mb': 250, 'database_stop_mb': 400,
            'note': 'Estimate only; verify actual size with usage after a full scan.'}
