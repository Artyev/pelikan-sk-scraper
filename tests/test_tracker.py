import copy
import json
from datetime import date
from pathlib import Path

import pytest

from pelikan_tracker.config import load_config, queries
from pelikan_tracker.http import FetchError
from pelikan_tracker.normalize import SchemaError, normalize
from pelikan_tracker.pelikan import PelikanClient, search_payload, search_url
from pelikan_tracker.storage import CapacityError, SupabaseStore, save_outbox


@pytest.fixture
def config():
    return load_config('config.json')


@pytest.fixture
def page():
    return json.loads(Path('tests/fixtures/vie_rom_2027-02-18.json').read_text())


def parse(page, config, airport=None):
    return normalize([page], airport or {'iata': 'VIE', 'timezone': 'Europe/Vienna'},
                     date(2027, 2, 18), config, 'https://www.pelikan.sk/')


def test_live_fixture(page, config):
    offers, excluded = parse(page, config)
    assert len(offers) == 6
    assert excluded['other_cabin'] == 2
    eligible = [o for o in offers if o['eligible_for_event']]
    assert len(eligible) == 1
    assert eligible[0]['marketing_carrier'] == 'FR'
    assert eligible[0]['flight_number'] == '1645'
    assert eligible[0]['price_amount'] == '47.99'
    assert eligible[0]['seats_available'] is None
    assert eligible[0]['cabin_baggage'] is None
    assert eligible[0]['arrival_at'] == '2027-02-18T07:55:00+01:00'


@pytest.mark.parametrize('hour,minute,expected', [(17, 0, True), (17, 1, False), (20, 0, False)])
def test_arrival_cutoff(page, config, hour, minute, expected):
    page['flights'] = page['flights'][:1]
    page['flights'][0]['thereTrips'][0]['segments'][0]['arrivalDateTime'] = [2027, 2, 18, hour, minute]
    offers, _ = parse(page, config)
    assert offers[0]['eligible_for_event'] is expected


def test_timezone_istanbul_and_overnight(page, config):
    page['flights'] = page['flights'][:1]
    segment = page['flights'][0]['thereTrips'][0]['segments'][0]
    segment['from']['airportCode'] = 'IST'
    segment['departureDateTime'] = [2027, 2, 18, 15, 0]
    segment['arrivalDateTime'] = [2027, 2, 18, 16, 0]
    offers, _ = parse(page, config, {'iata': 'IST', 'timezone': 'Europe/Istanbul'})
    assert offers[0]['duration_minutes'] == 180
    segment['arrivalDateTime'] = [2027, 2, 19, 0, 30]
    offers, _ = parse(page, config, {'iata': 'IST', 'timezone': 'Europe/Istanbul'})
    assert not offers[0]['eligible_for_event']


def test_connections_techstops_and_wrong_date(page, config):
    flight = page['flights'][0]
    page['flights'] = [flight]
    trip = flight['thereTrips'][0]
    trip['segments'].append(copy.deepcopy(trip['segments'][0]))
    assert parse(page, config)[1]['connections'] == 1
    trip['segments'].pop()
    trip['segments'][0]['techStops'] = ['MXP']
    assert parse(page, config)[1]['technical_stops'] == 1
    trip['segments'][0]['techStops'] = []
    trip['segments'][0]['departureDateTime'][2] = 17
    assert parse(page, config)[1]['wrong_route_or_date'] == 1


def test_prior_days_and_cia(page, config):
    page['flights'] = page['flights'][:1]
    segment = page['flights'][0]['thereTrips'][0]['segments'][0]
    segment['to']['airportCode'] = 'CIA'
    segment['departureDateTime'] = [2027, 2, 16, 20, 0]
    segment['arrivalDateTime'] = [2027, 2, 16, 22, 0]
    offers, _ = normalize([page], {'iata': 'VIE', 'timezone': 'Europe/Vienna'}, date(2027, 2, 16), config, 'url')
    assert offers[0]['eligible_for_event']


def test_schema_drift_not_empty(page, config):
    del page['flights'][0]['price']
    with pytest.raises(SchemaError):
        parse(page, config)
    with pytest.raises(SchemaError):
        parse({'valid': False, 'flights': []}, config)
    assert parse({'valid': True, 'flights': []}, config)[0] == []


@pytest.mark.parametrize('price', ['NaN', '-1', '1.001', 'Infinity'])
def test_invalid_money(page, config, price):
    page['flights'][0]['price'] = price
    with pytest.raises(SchemaError):
        parse(page, config)


def test_stable_identity_and_history(page, config):
    before, _ = parse(page, config)
    page['flights'][0]['id'] = 999999
    after, _ = parse(page, config)
    assert before[0]['offer_key'] == after[0]['offer_key']
    page['flights'][0]['price'] = 46.99
    changed, _ = parse(page, config)
    assert before[0]['fare_key'] == changed[0]['fare_key']
    assert before[0]['offer_key'] != changed[0]['offer_key']


def test_queries(config):
    assert len(list(queries(config))) == 90
    assert len(list(queries(config, 'VIE'))) == 3
    with pytest.raises(ValueError):
        list(queries(config, 'FCO'))
    with pytest.raises(ValueError):
        list(queries(config, day='2027-02-19'))
    payload = search_payload('VIE', date(2027, 2, 18))
    assert payload['travelClass'] == 'ECONOMY'
    assert payload['rangeDates'] is False
    assert 'back' not in payload
    assert 'CDT:CROM' in search_url('VIE', date(2027, 2, 18))


class FakeHTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client(config, responses):
    http = FakeHTTP([{'sessionToken': 'test-only'}, *responses])
    c = PelikanClient(config, http)
    c.api = 'https://www.pelikan.sk/gf1/pelijee-sk/'
    return c, http


def test_pagination(config):
    full = {'valid': True, 'flights': [{'id': i} for i in range(10)], 'total': 10}
    empty = {'valid': True, 'flights': []}
    c, http = client(config, [full, empty])
    assert len(c.search('VIE', date(2027, 2, 18))) == 2
    assert 'offset=10' in http.calls[-1][0]
    c, _ = client(config, [full, full])
    with pytest.raises(FetchError, match='Repeated'):
        c.search('VIE', date(2027, 2, 18))
    c, _ = client({**config, 'max_pages': 1}, [full])
    with pytest.raises(FetchError, match='incomplete'):
        c.search('VIE', date(2027, 2, 18))


def test_search_error_not_empty(config):
    c, _ = client(config, [{'errorType': 'INTERNAL_SERVER_ERROR'}])
    with pytest.raises(FetchError):
        c.search('VIE', date(2027, 2, 18))


def test_outbox_retry_preserves_uuid(tmp_path):
    snapshot = {'schema_version': 1, 'search': {'id': 'test-uuid'}, 'offers': []}
    path = save_outbox(tmp_path, snapshot)
    http = FakeHTTP([FetchError('offline'), 'test-uuid'])
    store = SupabaseStore('https://test.supabase.co', 'sb_secret_test', http)
    with pytest.raises(FetchError):
        store.upload(path)
    assert path.exists()
    store.upload(path)
    assert not path.exists()
    assert (tmp_path / 'sent' / 'test-uuid.json').exists()
    assert http.calls[0][1]['payload'] == http.calls[1][1]['payload']
    assert 'Authorization' not in http.calls[0][1]['headers']


def test_legacy_key():
    store = SupabaseStore('https://test.supabase.co', 'legacy-test')
    assert store.headers['Authorization'] == 'Bearer legacy-test'


def test_unexpected_rpc_response_keeps_outbox(tmp_path):
    path = save_outbox(tmp_path, {'search': {'id': 'test-uuid'}, 'offers': []})
    store = SupabaseStore('https://test.supabase.co', 'sb_secret_test', FakeHTTP(['wrong-id']))
    with pytest.raises(FetchError, match='acknowledge'):
        store.upload(path)
    assert path.exists()


def test_capacity_preserves_full_local_evidence(tmp_path):
    snapshot = {'search': {'id': 'test-uuid'}, 'offers': [{'raw': {'segment': 'evidence'}, 'price_amount': '12.00'}]}
    path = save_outbox(tmp_path, snapshot)
    http = FakeHTTP([None])
    store = SupabaseStore('https://test.supabase.co', 'sb_secret_test', http)
    with pytest.raises(CapacityError):
        store.upload(path)
    assert json.loads(path.read_text()) == snapshot
    assert http.calls[0][1]['payload']['p_snapshot']['offers'][0]['raw'] == {}


def test_campaign_schedule_and_stop(tmp_path, config):
    from datetime import datetime, timedelta, timezone
    from pelikan_tracker.campaign import tick
    start = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    calls = []
    def collect(end):
        calls.append(end)
        return 0
    path = tmp_path / 'campaign.json'
    for hour in range(137 * 24 + 48):
        tick(path, config, collect, start + timedelta(hours=hour))
    assert len(calls) == 274
    assert set(calls) == {(start + timedelta(days=137)).isoformat()}


def test_campaign_failed_slot_and_restart(tmp_path, config):
    from datetime import datetime, timedelta, timezone
    from pelikan_tracker.campaign import tick
    start = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    path = tmp_path / 'campaign.json'
    calls = []
    def fail(end):
        calls.append(end)
        return 1
    assert tick(path, config, fail, start) == 1
    assert tick(path, config, fail, start + timedelta(minutes=10)) == 0
    tick(path, config, fail, start + timedelta(days=2))
    assert len(calls) == 2  # No burst of backfilled historical snapshots.
    with pytest.raises(ValueError, match='another event'):
        tick(path, {**config, 'event_id': 'another-event'}, fail, start)


def test_storage_estimate(config):
    from pelikan_tracker.campaign import estimate
    result = estimate(config, 90, 20)
    assert result['runs'] == 274
    assert result['searches'] == 24660
    assert result['observation_rows'] == 493200
    assert result['estimated_flight_mb'] == 1035.72


@pytest.mark.parametrize('error', [CapacityError('budget'), FetchError('offline')])
def test_run_continues_local_collection_when_cloud_unavailable(tmp_path, monkeypatch, error):
    import pelikan_tracker.cli as cli
    calls = []
    class FakeStore:
        def upload(self, path):
            raise error
    class FakeClient:
        def __init__(self, config):
            pass
        def search(self, airport, day):
            calls.append(day)
            return [{'valid': True, 'flights': [], 'total': 0}]
    monkeypatch.setattr(cli, 'SupabaseStore', FakeStore)
    monkeypatch.setattr(cli, 'PelikanClient', FakeClient)
    assert cli.main(['--outbox', str(tmp_path), 'run', '--origin', 'VIE']) == 1
    assert len(calls) == 3
    assert len(list(tmp_path.glob('*.json'))) == 3


def test_campaign_lock_prevents_overlap(tmp_path, config):
    from datetime import datetime, timezone
    from pelikan_tracker.campaign import tick
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    path = tmp_path / 'state.json'
    def never(end):
        pytest.fail('Concurrent collection was started')
    def collect(end):
        return tick(path, config, never, now)
    assert tick(path, config, collect, now) == 0
