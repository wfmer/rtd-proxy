# RTD Denver GTFS-RT Proxy

A lightweight JSON proxy for RTD Denver's real-time transit feed, built for the [RTD Denver Tidbyt app](https://github.com/tidbyt/community/pull/3178).

RTD's real-time data is published as [GTFS-Realtime](https://gtfs.org/realtime/) protobuf — a binary format that can't be parsed in Tidbyt's Starlark environment. This proxy fetches the protobuf feed, joins it with static GTFS data for stop names, and exposes a simple JSON API.

## API

| Endpoint | Description |
|---|---|
| `GET /predictions/<stop_id>` | Real-time arrivals for a stop |
| `GET /health` | Health check |
| `GET /` | Service info |

### Example

```
GET /predictions/11981
```

```json
{
  "stop_id": "11981",
  "stop_name": "Alameda Ave & S Downing St",
  "predictions": [
    { "route": "3", "headsign": "3", "minutes": 4 },
    { "route": "3", "headsign": "3", "minutes": 19 }
  ],
  "count": 2,
  "timestamp": 1778392868
}
```

Find your stop ID at [rtd-denver.com](https://www.rtd-denver.com) or on the stop sign post.

## Data sources

- **Real-time feed**: `https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb` — cached 30s
- **Static GTFS** (stop names): `https://www.rtd-denver.com/files/gtfs/google_transit.zip` — cached 24h

## Running locally

```bash
pip install -r requirements.txt
python main.py
```

The server listens on `:8080` by default. Set `PORT` to override.

## Deployment (Cloud Run)

```bash
# Build and push
docker build -t gcr.io/<PROJECT>/rtd-proxy .
docker push gcr.io/<PROJECT>/rtd-proxy

# Deploy
gcloud run deploy rtd-proxy \
  --image gcr.io/<PROJECT>/rtd-proxy \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 1
```

`--min-instances 1` keeps the instance warm so the first request doesn't cold-start mid-render on a Tidbyt device.
