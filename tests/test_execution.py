import json

import pytest

from pelikan_tracker import cli
from pelikan_tracker.http import BlockedError, FetchError, HTTPClient


@pytest.mark.parametrize('content', [b'<html>reCAPTCHA</html>',
                                     b'<html>Verify you are human</html>'])
def test_challenge_http_200(monkeypatch, content):
    client = HTTPClient()
    monkeypatch.setattr(client, 'request', lambda *args, **kwargs: content)
    with pytest.raises(BlockedError):
        client.json('url')


def test_regular_json_and_non_json(monkeypatch):
    client = HTTPClient()
    monkeypatch.setattr(client, 'request', lambda *args, **kwargs: b'{"note":"captcha"}')
    assert client.json('url') == {'note': 'captcha'}
    monkeypatch.setattr(client, 'request', lambda *args, **kwargs: b'<html>Maintenance</html>')
    with pytest.raises(FetchError) as exc:
        client.json('url')
    assert not isinstance(exc.value, BlockedError)


def test_repeated_collection_appends_and_records_trigger(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, config):
            pass

        def search(self, *args):
            return [{'valid': True, 'flights': []}]

    monkeypatch.setattr(cli, 'PelikanClient', FakeClient)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    monkeypatch.setenv('GITHUB_RUN_ID', '12345')
    args = ['--outbox', str(tmp_path), 'run', '--origin', 'VIE',
            '--date', '2027-02-18', '--local-only']
    assert cli.main(args) == 0
    original = next(tmp_path.glob('*.json'))
    original_content = original.read_bytes()
    assert cli.main(args) == 0
    snapshots = [json.loads(path.read_text()) for path in tmp_path.glob('*.json')]
    assert len(snapshots) == 2
    assert original.read_bytes() == original_content
    assert len({item['search']['id'] for item in snapshots}) == 2
    assert len({item['search']['run_id'] for item in snapshots}) == 2
    assert all(item['search']['diagnostics']['execution']['trigger'] == 'schedule'
               for item in snapshots)


def test_blocked_scan_saves_failure_and_stops(tmp_path, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, config):
            pass

        def search(self, *args):
            calls.append(args)
            raise BlockedError('CAPTCHA/challenge page')

    monkeypatch.setattr(cli, 'PelikanClient', FakeClient)
    assert cli.main(['--outbox', str(tmp_path), 'run', '--local-only']) == 1
    assert len(calls) == 1
    snapshot = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert snapshot['search']['status'] == 'blocked'
    assert snapshot['offers'] == []
