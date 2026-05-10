#!/usr/bin/env python3
"""
RTD GTFS-RT Proxy for Tidbyt
Converts RTD's protobuf GTFS-RT feed to JSON for Tidbyt apps
Designed for Google Cloud Run deployment
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
    'stops': None,       # dict: stop_id -> stop_name
    'timestamp': 0,
    'ttl': 86400,        # Static GTFS: refresh once per day
}

# ── RTD feed URLs ──────────────────────────────────────────────────────────────
RTD_TRIP_UPDATE_URL  = "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb"
RTD_STATIC_GTFS_URL  = "https://www.rtd-denver.com/files/gtfs/google_transit.zip"


def fetch_static_stops():
    """
    Fetch stop names from RTD's static GTFS zip.
    Returns a dict of {stop_id: stop_name}, cached for 24 hours.
    """
    current_time = time.time()
    if (static_cache['stops'] is not None and
            (current_time - static_cache['timestamp']) < static_cache['ttl']):
        return static_cache['stops']

    stops = {}
    try:
        resp = requests.get(RTD_STATIC_GTFS_URL, timeout=20)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open('stops.txt') as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    stops[row['stop_id']] = row.get('stop_name', row['stop_id'])

        static_cache['stops'] = stops
        static_cache['timestamp'] = current_time
        print(f"Loaded {len(stops)} stops from static GTFS")
    except Exception as e:
        print(f"Error fetching static GTFS: {e}")
        # Fall back to whatever we had (could be None)
        if static_cache['stops']:
            stops = static_cache['stops']

    return stops


def fetch_gtfs_rt_data():
    """
    Fetch and parse GTFS-RT protobuf data from RTD.
    Returns a dict of {stop_id: [predictions]}.
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
                # trip_headsign is not in GTFS-RT trip updates; use route_id as label
                headsign = route_id

                for stop_time_update in trip_update.stop_time_update:
                    stop_id = stop_time_update.stop_id

                    if stop_time_update.HasField('arrival'):
                        arrival_time = stop_time_update.arrival.time
                        minutes = int((arrival_time - current_time) / 60)

                        if minutes >= 0:
                            if stop_id not in predictions_by_stop:
                                predictions_by_stop[stop_id] = []

                            predictions_by_stop[stop_id].append({
                                'route': route_id,
                                'headsign': headsign,
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


@app.route('/predictions/<stop_id>')
def get_predictions(stop_id):
    """Get predictions for a specific stop, including the stop's human-readable name."""
    try:
        all_predictions = fetch_gtfs_rt_data()
        stops = fetch_static_stops()

        stop_predictions = all_predictions.get(stop_id, [])

        # Clean up: strip arrival_time (internal only) from response
        clean_predictions = [
            {k: v for k, v in p.items() if k != 'arrival_time'}
            for p in stop_predictions[:10]
        ]

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
        'example': '/predictions/11981',
        'health': '/health',
        'data_source': 'RTD Denver GTFS-Realtime',
        'cache_ttl': rt_cache['ttl'],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
