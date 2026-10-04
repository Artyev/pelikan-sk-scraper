import json
import socket
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class FetchError(RuntimeError):
    pass


class BlockedError(FetchError):
    pass


class HTTPClient:
    def __init__(self, interval=5, timeout=60):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.interval, self.timeout, self.last_request = interval, timeout, 0

    def request(self, url, *, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request_headers = {'Accept': 'application/json', 'User-Agent': 'PelikanRomeTracker/0.1'}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        for attempt in range(3):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.opener.open(Request(url, data=body, headers=request_headers), timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                # Do not include URLs, session tokens, headers or response bodies in logs.
                if exc.code in (401, 403, 429):
                    raise BlockedError(f'HTTP {exc.code}; stopping, no automatic bypass') from None
                if exc.code not in (500, 502, 503, 504) or attempt == 2:
                    raise FetchError(f'HTTP {exc.code}') from None
            except (URLError, TimeoutError, socket.timeout):
                if attempt == 2:
                    raise FetchError('Network failure after 3 attempts') from None
            time.sleep(2 ** attempt)
        raise FetchError('Request failed')

    def json(self, url, **kwargs):
        try:
            return json.loads(self.request(url, **kwargs))
        except (ValueError, UnicodeError):
            raise FetchError('Expected JSON; possible site change or challenge page') from None
