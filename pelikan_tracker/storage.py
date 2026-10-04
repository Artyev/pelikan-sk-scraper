"""Durable local outbox + idempotent, atomic Supabase RPC delivery."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .http import FetchError, HTTPClient


class CapacityError(FetchError):
    pass


def save_outbox(directory, snapshot):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (snapshot['search']['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(snapshot, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    return path


class SupabaseStore:
    def __init__(self, url=None, key=None, http=None):
        url = (url or os.environ.get('SUPABASE_URL', '')).rstrip('/')
        key = key or os.environ.get('SUPABASE_SECRET_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or not key:
            raise ValueError('Set SUPABASE_URL (https) and SUPABASE_SECRET_KEY in the environment')
        self.url = url + '/rest/v1/rpc/ingest_flight_snapshot'
        self.headers = {'apikey': key}
        # New secret keys are not JWTs and must not be sent as Bearer tokens.
        if not key.startswith('sb_secret_'):
            self.headers['Authorization'] = 'Bearer ' + key
        self.http = http or HTTPClient(interval=0, timeout=30)

    def upload(self, path):
        path = Path(path)
        snapshot = json.loads(path.read_text())
        # Keep full source evidence on disk; avoid duplicating raw JSON in every DB row.
        compact = {**snapshot, 'offers': [{**offer, 'raw': {}} for offer in snapshot['offers']]}
        result = self.http.json(self.url, payload={'p_snapshot': compact}, headers=self.headers)
        # Migration 002 returns JSON null without inserting when a budget would be exceeded.
        if result is None:
            raise CapacityError('Database storage budget reached; retained locally')
        if result != snapshot['search']['id']:
            raise FetchError('Supabase did not acknowledge the expected snapshot ID')
        sent = path.parent / 'sent'
        sent.mkdir(exist_ok=True)
        path.replace(sent / path.name)

    def usage(self):
        return self.http.json(self.url.replace('ingest_flight_snapshot', 'flight_storage_usage'),
                              payload={}, headers=self.headers)
