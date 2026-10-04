import base64
import hashlib
import json
from urllib.parse import urlencode

from .http import FetchError, HTTPClient

SITE = 'https://www.pelikan.sk/'
HEADERS = {'appType': 'desktop', 'X-App-Language': 'sk', 'Referer': SITE}


def search_url(origin, day):
    return SITE + f'sk/letenky/T:2,P:1000E,CDF:A{origin},CDT:CROM,DD:{day.year}_{day.month}_{day.day},R:0,LOAD'


def search_payload(origin, day):
    return {
        'there': {'date': [day.year, day.month, day.day],
                  'from': {'code': origin, 'type': 'AIRPORT'},
                  'to': {'code': 'ROM', 'type': 'CITY'}},
        'passengerCount': {'adt': 1, 'yth': 0, 'chd': 0, 'inf': 0, 'ythYears': [], 'chdYears': []},
        'travelClass': 'ECONOMY', 'rangeDates': False,
        'filterQuery': {'sortBy': 'PRICE', 'filters': [
            {'name': 'FLIGHT_STOPS', 'values': [{'code': 'ZERO', 'checked': True}]}
        ], 'rangeFilters': []},
    }


class PelikanClient:
    def __init__(self, config, http=None):
        self.http = http or HTTPClient(config['request_interval_seconds'], config['timeout_seconds'])
        self.max_pages = config['max_pages']
        self.api = None
        self.currency = None

    def initialize(self):
        try:
            wrapped = self.http.json(SITE + 'desktop-docker/config.json')
            config = json.loads(base64.b64decode(wrapped['data']))
            api = config['server']['apiUrl']
            if not api.startswith(SITE + 'gf1/'):
                raise ValueError('Unexpected API host/path')
            if config['market']['currency']['code'] != 'EUR':
                raise ValueError('Unexpected currency')
            self.api, self.currency = api, 'EUR'
        except (KeyError, TypeError, ValueError):
            raise FetchError('Pelikan configuration schema changed') from None

    def search(self, origin, day):
        if self.api is None:
            self.initialize()
        # Each route/date gets a fresh anonymous session: cached result IDs are session-scoped.
        session = self.http.json(self.api + 'admin/getsession', headers=HEADERS)
        if not isinstance(session, dict) or not session.get('sessionToken'):
            raise FetchError('Missing anonymous search session')
        payload = search_payload(origin, day)
        pages, signatures = [], set()
        for index in range(self.max_pages):
            params = dict(limit=10, offset=index * 10, tdepc=origin, tdesc='ROM',
                          theredate=day.isoformat(), adt=1, yth=0, chd=0, inf=0,
                          tabIdentifier=session['sessionToken'])
            page = self.http.json(self.api + 'mobile/search?' + urlencode(params), payload=payload, headers=HEADERS)
            if not isinstance(page, dict) or page.get('valid') is not True or not isinstance(page.get('flights'), list):
                raise FetchError('Invalid Pelikan search response (not a zero-result search)')
            flights = page['flights']
            signature = hashlib.sha256(json.dumps(flights, sort_keys=True).encode()).hexdigest()
            if flights and signature in signatures:
                raise FetchError('Repeated search page; completeness cannot be confirmed')
            signatures.add(signature)
            pages.append(page)
            # The website uses batches of 10. A short final page confirms completion;
            # total is retained for diagnostics but is not trusted for early stopping.
            if len(flights) < 10:
                return pages
        raise FetchError('Page limit reached; search is incomplete')
