#!/usr/bin/env python3
"""
RTD GTFS-RT Proxy for Tidbyt
Converts RTD's protobuf GTFS-RT feed to JSON for Tidbyt apps
Designed for Google Cloud Run deployment

Enriches realtime arrivals with static-GTFS data so the Tidbyt app can show
real destinations and per-route brand colors:
  - trip_id  -> trip_headsign   (trips.txt)   e.g. "Union Station"
  - route_id -> route_short_name (routes.txt)  e.g. "L"
  - route_id -> route_color / route_text_color (routes.txt)
"""

from flask import Flask, jsonify
from google.transit import gtfs_realtime_pb2
import requests
import time
import os
import io
import csv
import zipfile

app = Flask(__name__)

# ── Cache buckets ──────────────────────────────────────────────────────────────
rt_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 30,           # Realtime: refresh every 30 s
}

static_cache = {
    'stops': None,       # dict: stop_id  -> stop_name
    'trips': None,       # dict: trip_id  -> trip_headsign
    'routes': None,      # dict: route_id -> {"short", "color", "text_color"}
    'timestamp': 0,
    'ttl': 86400,        # Static GTFS: refresh once per day
}

# ── RTD feed URLs ──────────────────────────────────────────────────────────────
RTD_TRIP_UPDATE_URL  = "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb"
# NOTE: this 308-redirects to /api/download?...; requests follows redirects by default.
RTD_STATIC_GTFS_URL  = "https://www.rtd-denver.com/files/gtfs/google_transit.zip"


def _norm_color(value):
    """Normalize a GTFS color ('0076CE', no #) to '#0076CE'. Returns None if invalid."""
    if not value:
        return None
    v = value.strip().lstrip('#')
    if len(v) == 6 and all(c in '0123456789abcdefABCDEF' for c in v):
        return '#' + v.upper()
    return None


def fetch_static_gtfs():
    """
    Fetch stop names, trip headsigns, and route branding from RTD's static GTFS zip.
    Reads only the three small files it needs (stops/trips/routes), never the
    large stop_times.txt / shapes.txt. Cached for 24 hours.

    Returns (stops, trips, routes):
      stops:  {stop_id: stop_name}
      trips:  {trip_id: trip_headsign}
      routes: {route_id: {"short": str, "color": str|None, "text_color": str|None}}
    """
    current_time = time.time()
    if (static_cache['stops'] is not None and
            (current_time - static_cache['timestamp']) < static_cache['ttl']):
        return static_cache['stops'], static_cache['trips'], static_cache['routes']

    stops, trips, routes = {}, {}, {}
    try:
        resp = requests.get(RTD_STATIC_GTFS_URL, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open('stops.txt') as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding='utf-8')):
                    stops[row['stop_id']] = row.get('stop_name') or row['stop_id']

            with zf.open('trips.txt') as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding='utf-8')):
                    headsign = (row.get('trip_headsign') or '').strip()
                    if headsign:
                        trips[row['trip_id']] = headsign

            with zf.open('routes.txt') as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding='utf-8')):
                    routes[row['route_id']] = {
                        'short': (row.get('route_short_name') or '').strip(),
                        'color': _norm_color(row.get('route_color')),
                        'text_color': _norm_color(row.get('route_text_color')),
                    }

        static_cache['stops'] = stops
        static_cache['trips'] = trips
        static_cache['routes'] = routes
        static_cache['timestamp'] = current_time
        print(f"Loaded static GTFS: {len(stops)} stops, {len(trips)} trips, {len(routes)} routes")
    except Exception as e:
        print(f"Error fetching static GTFS: {e}")
        # Fall back to whatever we had cached (could be None dicts on cold start)
        if static_cache['stops'] is not None:
            return static_cache['stops'], static_cache['trips'], static_cache['routes']

    return stops, trips, routes


def fetch_gtfs_rt_data():
    """
    Fetch and parse GTFS-RT protobuf data from RTD.
    Returns a dict of {stop_id: [raw predictions]} where each raw prediction is
    {route_id, trip_id, minutes, arrival_time}. Enrichment (headsign, colors)
    happens at request time so the 30 s RT cache stays independent of static GTFS.
    """
    current_time = time.time()

    if rt_cache['data'] and (current_time - rt_cache['timestamp']) < rt_cache['ttl']:
        return rt_cache['data']

    try:
        response = requests.get(RTD_TRIP_UPDATE_URL, timeout=10)
        response.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        predictions_by_stop = {}

        for entity in feed.entity:
            if entity.HasField('trip_update'):
                trip_update = entity.trip_update
                route_id = trip_update.trip.route_id
                trip_id = trip_update.trip.trip_id

                for stop_time_update in trip_update.stop_time_update:
                    stop_id = stop_time_update.stop_id

                    if stop_time_update.HasField('arrival'):
                        arrival_time = stop_time_update.arrival.time
                        minutes = int((arrival_time - current_time) / 60)

                        if minutes >= 0:
                            predictions_by_stop.setdefault(stop_id, []).append({
                                'route_id': route_id,
                                'trip_id': trip_id,
                                'minutes': minutes,
                                'arrival_time': arrival_time,
                            })

        # Sort by arrival time
        for stop_id in predictions_by_stop:
            predictions_by_stop[stop_id].sort(key=lambda x: x['arrival_time'])

        rt_cache['data'] = predictions_by_stop
        rt_cache['timestamp'] = current_time

        return predictions_by_stop

    except Exception as e:
        print(f"Error fetching GTFS-RT data: {e}")
        return rt_cache['data'] if rt_cache['data'] else {}


def _enrich(raw, trips, routes):
    """Turn a raw RT prediction into the JSON shape the Tidbyt app consumes."""
    route_id = raw['route_id']
    route_meta = routes.get(route_id, {})

    # Badge label: prefer the human route short name ("L", "15"); fall back to id.
    label = route_meta.get('short') or route_id
    # Destination: real headsign from the static trip; fall back to the route label.
    headsign = trips.get(raw['trip_id']) or label

    return {
        'route': label,
        'headsign': headsign,
        'minutes': raw['minutes'],
        'color': route_meta.get('color'),            # e.g. "#0076CE" or None
        'text_color': route_meta.get('text_color'),  # e.g. "#FFFFFF" or None
    }


@app.route('/predictions/<stop_id>')
def get_predictions(stop_id):
    """Get predictions for a specific stop, including the stop's human-readable name."""
    try:
        all_predictions = fetch_gtfs_rt_data()
        stops, trips, routes = fetch_static_gtfs()

        raw = all_predictions.get(stop_id, [])[:10]
        clean_predictions = [_enrich(p, trips, routes) for p in raw]

        stop_name = stops.get(stop_id, "")

        return jsonify({
            'stop_id': stop_id,
            'stop_name': stop_name,
            'timestamp': int(time.time()),
            'count': len(clean_predictions),
            'predictions': clean_predictions,
        })

    except Exception as e:
        return jsonify({
            'error': str(e),
            'stop_id': stop_id,
            'stop_name': '',
            'timestamp': int(time.time()),
            'count': 0,
            'predictions': [],
        }), 500


@app.route('/health')
def health():
    """Health check endpoint for Cloud Run"""
    return jsonify({'status': 'healthy', 'timestamp': int(time.time())})


@app.route('/')
def index():
    """Root endpoint with usage instructions"""
    return jsonify({
        'service': 'RTD GTFS-RT Proxy for Tidbyt',
        'usage': 'GET /predictions/<stop_id>',
        'example': '/predictions/33727',
        'health': '/health',
        'data_source': 'RTD Denver GTFS-Realtime',
        'cache_ttl': rt_cache['ttl'],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
