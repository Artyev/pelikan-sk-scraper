"""Ephemeral GitHub runner: fixed campaign window and durable pending-only artifacts."""
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from .cli import main as tracker_main
from .config import load_config
from .http import FetchError
from .storage import SupabaseStore

ARTIFACT_PREFIX = 'pelikan-pending-v1-'


def campaign_window(start_text, days, current=None):
    if not start_text:
        raise ValueError('Set repository variable CAMPAIGN_START_UTC, for example 2026-10-05T00:00:00Z')
    start = datetime.fromisoformat(start_text)
    if start.tzinfo is None:
        raise ValueError('CAMPAIGN_START_UTC must include a timezone (Z or +00:00)')
    end = start + timedelta(days=days)
    current = current or datetime.now(timezone.utc)
    return start <= current < end, end


def gh_api(endpoint, method='GET'):
    result = subprocess.run(['gh', 'api', '--method', method, endpoint], capture_output=True, timeout=90)
    if result.returncode:
        raise FetchError('GitHub artifact API failed; original artifacts retained')
    return result.stdout


def pending_artifacts(repository, api=gh_api):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GITHUB_REPOSITORY')
    artifacts = []
    page = 1
    while True:
        result = json.loads(api(f'repos/{repository}/actions/artifacts?per_page=100&page={page}'))
        items = result['artifacts']
        artifacts.extend(item for item in items if item['name'].startswith(ARTIFACT_PREFIX) and not item['expired'])
        if len(items) < 100:
            return sorted(artifacts, key=lambda item: item['created_at'])
        page += 1


def restore_zip(content, directory):
    """Accept only flat UUID-named snapshot JSON files; never extract arbitrary paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        if not files or sum(item.file_size for item in files) > 100_000_000:
            raise ValueError('Empty or oversized pending artifact; original retained')
        paths = []
        for item in files:
            name = item.filename
            if not re.fullmatch(r'[a-f0-9-]{36}\.json', name):
                raise ValueError('Unexpected artifact filename')
            identifier = str(UUID(name[:-5]))
            snapshot = json.loads(archive.read(item))
            if snapshot.get('schema_version') != 1 or snapshot['search']['id'] != identifier:
                raise ValueError('Invalid snapshot in artifact')
            path = directory / name
            path.write_text(json.dumps(snapshot, ensure_ascii=False))
            paths.append(path)
        return paths


def recover(repository, store, directory='data/cloud-recovery', api=gh_api):
    for artifact in pending_artifacts(repository, api):
        identifier = int(artifact['id'])
        content = api(f'repos/{repository}/actions/artifacts/{identifier}/zip')
        paths = restore_zip(content, Path(directory) / str(identifier))
        # Partial success is safe: the unchanged original artifact remains until all ACKs arrive.
        # Re-uploading acknowledged UUIDs is idempotent in the Supabase RPC.
        for path in paths:
            store.upload(path)
        api(f'repos/{repository}/actions/artifacts/{identifier}', method='DELETE')
        print(f'Delivered and removed pending artifact {identifier}', flush=True)


def main(argv=None):
    argv = argv or sys.argv[1:]
    try:
        config = load_config('config.json')
        mode = os.environ.get('COLLECTION_MODE', 'collect')
        if mode not in {'check', 'collect', 'sync'}:
            raise ValueError('Invalid COLLECTION_MODE')
        if mode == 'collect':
            active, end = campaign_window(os.environ.get('CAMPAIGN_START_UTC'), config['collection_days'])
        else:
            active, end = True, None
        if argv == ['gate']:
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(f'active={str(active).lower()}\n')
            print('Campaign active' if active else 'Outside collection window; no search')
            return 0
        if argv != ['run']:
            raise ValueError('Use gate or run')
        if not active:
            return 0
        store = SupabaseStore()
        failures = 0
        try:
            recover(os.environ['GITHUB_REPOSITORY'], store)
        except (FetchError, ValueError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
            print(f'Pending delivery failed: {type(exc).__name__}. Original artifacts retained.', file=sys.stderr)
            failures = 1
        if mode == 'sync':
            return failures
        options = ['--outbox', 'data/cloud-new', 'run']
        if mode == 'check':
            options += ['--origin', 'VIE', '--date', config['end_date']]
        else:
            options += ['--until', end.isoformat()]
        return max(failures, tracker_main(options))
    except KeyboardInterrupt:
        print('Time limit reached; remaining searches were not completed. Saving completed pending snapshots.', file=sys.stderr)
        return 1
    except (FetchError, ValueError, KeyError, OSError) as exc:
        print(f'Cloud runner failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
