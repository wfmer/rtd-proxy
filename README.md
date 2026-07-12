# RTD Denver GTFS-RT Proxy

A lightweight JSON proxy for RTD Denver's real-time transit feed, built for the [RTD Denver Tidbyt app](https://github.com/tidbyt/community/pull/3178).

RTD's real-time data is published as [GTFS-Realtime](https://gtfs.org/realtime/) protobuf — a binary format that can't be parsed in Tidbyt's Starlark environment. This proxy fetches the protobuf feed, joins it with static GTFS data (stop names, trip headsigns, and per-route brand colors), and exposes a simple JSON API.

## API

| Endpoint | Description |
|---|---|
| `GET /predictions/<stop_id>` | Real-time arrivals for a stop |
| `GET /health` | Health check |
| `GET /` | Service info |

### Example

```
GET /predictions/33727
```

```json
{
  "stop_id": "33727",
  "stop_name": "Union Station",
  "predictions": [
    { "route": "L", "headsign": "L-Line 30th & Downing", "minutes": 3, "color": "#8F1838", "text_color": "#FFFFFF" },
    { "route": "W", "headsign": "Union Station", "minutes": 11, "color": "#009DAA", "text_color": "#FFFFFF" }
  ],
  "count": 2,
  "timestamp": 1778392868
}
```

`color` / `text_color` come from the route's static GTFS branding (`route_color` / `route_text_color`) and may be `null` if the route defines none. Buses share one RTD blue; rail lines are distinctly colored.

Find your stop ID at [rtd-denver.com](https://www.rtd-denver.com) or on the stop sign post.

## Data sources

- **Real-time feed**: `https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb` — cached 30s
- **Static GTFS** (stop names, trip headsigns, route colors): `https://www.rtd-denver.com/files/gtfs/google_transit.zip` — cached 24h. Only `stops.txt`, `trips.txt`, and `routes.txt` are read; the large `stop_times.txt`/`shapes.txt` are skipped.

## Running locally

```bash
pip install -r requirements.txt
python main.py
```

The server listens on `:8080` by default. Set `PORT` to override.

## Deployment (Cloud Run)

Deployed straight from source — Cloud Build builds the image from the `Dockerfile` and pushes it to Artifact Registry, so there's no manual `docker build`/`docker push` step:

```bash
gcloud run deploy rtd-proxy \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

The service scales to zero when idle (idle cost ≈ $0). Cold starts are mitigated by Cloud Run's startup-CPU-boost rather than a warm minimum instance; if cold-start latency ever bites a render, add `--min-instances 1` (which does incur a standing cost).
