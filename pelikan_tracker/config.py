import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text())
    config.setdefault('collection_days', 21)
    config.setdefault('collection_interval_hours', 12)
    if config['collection_days'] <= 0 or config['collection_interval_hours'] <= 0:
        raise ValueError('Collection duration and interval must be positive')
    start, end = date.fromisoformat(config['start_date']), date.fromisoformat(config['end_date'])
    event = datetime.fromisoformat(config['event_start'])
    if event.tzinfo is None or not start <= end <= event.astimezone(ZoneInfo('Europe/Rome')).date():
        raise ValueError('Invalid date window or event timezone')
    if not config['event_id'] or config['arrival_buffer_minutes'] < 0:
        raise ValueError('Invalid event or arrival buffer')
    if config['request_interval_seconds'] < 1 or config['timeout_seconds'] <= 0 or config['max_pages'] < 1:
        raise ValueError('Invalid request limits')
    codes = [x['iata'] for x in config['origins']]
    if not codes or len(set(codes)) != len(codes):
        raise ValueError('Origins must be nonempty and unique')
    for origin in config['origins']:
        if not re.fullmatch('[A-Z]{3}', origin['iata']) or origin['iata'] in {'FCO', 'CIA'}:
            raise ValueError('Invalid departure airport')
        ZoneInfo(origin['timezone'])
    return config


def queries(config: dict, origin: str | None = None, day: str | None = None):
    selected = [x for x in config['origins'] if origin is None or x['iata'] == origin]
    if not selected:
        raise ValueError('Origin is not in config.json')
    start, end = date.fromisoformat(config['start_date']), date.fromisoformat(config['end_date'])
    if day:
        requested = date.fromisoformat(day)
        if not start <= requested <= end:
            raise ValueError('Date is outside configured window')
        start = end = requested
    for airport in selected:
        current = start
        while current <= end:
            yield airport, current
            current += timedelta(days=1)
