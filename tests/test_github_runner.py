import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from pelikan_tracker.github_runner import campaign_window, pending_artifacts, recover, restore_zip
from pelikan_tracker.http import FetchError


IDENTIFIER = '9b2326f9-1c4e-4ef7-9b64-50e846848457'


def zip_snapshot(name=None, identifier=IDENTIFIER):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(name or IDENTIFIER + '.json', json.dumps({
            'schema_version': 1, 'search': {'id': identifier}, 'offers': []}))
    return buffer.getvalue()


def test_fixed_window_does_not_restart_with_new_machine():
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    active, end = campaign_window(start.isoformat(), 21, start)
    assert active
    assert end == start + timedelta(days=21)
    assert not campaign_window(start.isoformat(), 21, start - timedelta(seconds=1))[0]
    assert not campaign_window(start.isoformat(), 21, end)[0]
    with pytest.raises(ValueError):
        campaign_window('', 21)
    with pytest.raises(ValueError):
        campaign_window('2026-10-05T00:00:00', 21)


def test_restore_only_uuid_snapshots(tmp_path):
    files = restore_zip(zip_snapshot(), tmp_path)
    assert len(files) == 1
    assert json.loads(files[0].read_text())['search']['id'] == IDENTIFIER
    with pytest.raises(ValueError):
        restore_zip(zip_snapshot('../escape.json'), tmp_path)
    with pytest.raises(ValueError):
        restore_zip(zip_snapshot(identifier='wrong-id'), tmp_path)


def test_artifact_filtering():
    def api(endpoint):
        return json.dumps({'artifacts': [
            {'id': 1, 'name': 'another-project', 'expired': False, 'created_at': '2026-10-01'},
            {'id': 2, 'name': 'pelikan-pending-v1-2', 'expired': True, 'created_at': '2026-10-01'},
            {'id': 3, 'name': 'pelikan-pending-v1-3', 'expired': False, 'created_at': '2026-10-02'}
        ]}).encode()
    assert [item['id'] for item in pending_artifacts('owner/repo', api)] == [3]


@pytest.mark.parametrize('fails', [True, False])
def test_original_artifact_deleted_only_after_acknowledgment(tmp_path, fails):
    calls = []
    def api(endpoint, method='GET'):
        calls.append((endpoint, method))
        if endpoint.endswith('/zip'):
            return zip_snapshot()
        if method == 'DELETE':
            return b''
        return json.dumps({'artifacts': [
            {'id': 1, 'name': 'pelikan-pending-v1-123-1', 'expired': False, 'created_at': '2026-10-01'}
        ]}).encode()
    class Store:
        def upload(self, path):
            assert json.loads(path.read_text())['search']['id'] == IDENTIFIER
            if fails:
                raise FetchError('offline')
    if fails:
        with pytest.raises(FetchError):
            recover('owner/repo', Store(), tmp_path, api)
    else:
        recover('owner/repo', Store(), tmp_path, api)
    assert any(method == 'DELETE' for _, method in calls) is not fails
