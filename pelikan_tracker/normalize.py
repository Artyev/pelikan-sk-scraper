"""Parse the observed Pelikan desktop search contract, failing closed on schema drift."""
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


class SchemaError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def local_datetime(value, timezone):
    if not isinstance(value, list) or len(value) not in (5, 6) or any(type(v) is not int for v in value):
        raise SchemaError('Expected local date/time array')
    return datetime(*value, tzinfo=ZoneInfo(timezone))


def normalize(pages, airport, day, config, url):
    offers = {}
    excluded = {'connections': 0, 'technical_stops': 0, 'wrong_route_or_date': 0,
                'other_cabin': 0, 'non_aircraft': 0}
    event = datetime.fromisoformat(config['event_start']).astimezone(ZoneInfo('Europe/Rome'))
    deadline = event - timedelta(minutes=config['arrival_buffer_minutes'])
    try:
        for page in pages:
            if page.get('valid') is not True or not isinstance(page['flights'], list):
                raise SchemaError('Invalid search envelope')
            for flight in page['flights']:
                if flight.get('backTrips'):
                    raise SchemaError('Unexpected return itinerary in one-way search')
                trips = flight['thereTrips']
                if not isinstance(trips, list) or not trips:
                    raise SchemaError('Missing outbound trips')
                price = Decimal(str(flight['price']))
                if not price.is_finite() or price < 0 or price != price.quantize(Decimal('0.01')):
                    raise SchemaError('Invalid EUR price')
                for trip in trips:
                    segments = trip['segments']
                    if not isinstance(segments, list) or not segments:
                        raise SchemaError('Missing flight segments')
                    if len(segments) != 1:
                        excluded['connections'] += 1
                        continue
                    segment = segments[0]
                    if not isinstance(segment['techStops'], list):
                        raise SchemaError('Missing technical stop information')
                    if segment['techStops']:
                        excluded['technical_stops'] += 1
                        continue
                    if segment.get('equipmentStatus') != 'AIRCRAFT':
                        excluded['non_aircraft'] += 1
                        continue
                    if segment['travelClass'] != 'ECONOMY':
                        excluded['other_cabin'] += 1
                        continue
                    origin, destination = segment['from']['airportCode'], segment['to']['airportCode']
                    departure = local_datetime(segment['departureDateTime'], airport['timezone'])
                    arrival = local_datetime(segment['arrivalDateTime'], 'Europe/Rome')
                    if origin != airport['iata'] or destination not in {'FCO', 'CIA'} or departure.date() != day:
                        excluded['wrong_route_or_date'] += 1
                        continue
                    duration = (arrival.timestamp() - departure.timestamp()) / 60
                    if duration <= 0:
                        raise SchemaError('Arrival precedes departure')
                    available = flight['available']
                    trip_available = trip['available']
                    if type(available) is not bool or type(trip_available) is not bool:
                        raise SchemaError('Invalid availability flag')
                    carrier = segment['marketingCarrier']['code']
                    number = str(segment['flightNumber'])
                    if not carrier or not number:
                        raise SchemaError('Missing flight number')
                    cabin_baggage = segment.get('cabinBaggage') or None
                    checked_baggage = segment.get('checkedBaggage') or None
                    # Empty lists in live search are not reliable proof of no baggage.
                    fare_name = trip.get('brandedFare')
                    identity = [origin, destination, departure.isoformat(), carrier, number]
                    flight_key = fingerprint(identity)
                    fare_key = fingerprint([flight_key, 'ECONOMY', fare_name, cabin_baggage, checked_baggage])
                    raw = {'flight_id': flight['id'], 'trip_id': trip['id'], 'segment': segment,
                           'fare_name': fare_name, 'has_baggage_hint': flight.get('hasBaggage'),
                           'rules': trip.get('rules', [])}
                    # Preserve distinct offers at different prices without treating session IDs as stable.
                    key = fingerprint([fare_key, str(price), available, trip_available])
                    offers[key] = {
                        'offer_key': key, 'flight_key': flight_key, 'fare_key': fare_key,
                        'origin': origin, 'destination': destination,
                        'departure_at': departure.isoformat(), 'arrival_at': arrival.isoformat(),
                        'marketing_carrier': carrier,
                        'operating_carrier': segment.get('operatingCarrier', {}).get('code'),
                        'flight_number': number, 'duration_minutes': int(duration),
                        'aircraft': segment.get('aircraft'), 'cabin': 'ECONOMY', 'fare_name': fare_name,
                        'price_amount': str(price), 'currency': 'EUR', 'passengers': 1,
                        'available': available and trip_available,
                        'seats_available': None, 'seats_source': 'not_exposed_in_search',
                        'cabin_baggage': cabin_baggage, 'checked_baggage': checked_baggage,
                        'arrival_deadline': deadline.isoformat(), 'eligible_for_event': arrival <= deadline,
                        'minutes_until_event': int((event.timestamp() - arrival.timestamp()) / 60),
                        'search_url': url, 'raw': raw,
                    }
    except (KeyError, TypeError, InvalidOperation, OverflowError, IndexError) as exc:
        raise SchemaError(f'Pelikan response schema changed ({type(exc).__name__})') from None
    return sorted(offers.values(), key=lambda x: Decimal(x['price_amount'])), excluded
