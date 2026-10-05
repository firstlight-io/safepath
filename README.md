# SafePath Routing Engine

SafePath is a FastAPI + Leaflet project that compares two pedestrian routes between a start and end point:

- **Standard Route (Route A):** shortest path by physical distance
- **SafePath Route (Route B):** shortest path with heavy penalties on hazardous zones

The app demonstrates how dynamic edge weighting can trade off distance vs environmental exposure.

---

## What This Project Contains

- `/home/runner/work/safepath/safepath/main.py`  
  FastAPI server, request/response schemas, API endpoints, static hosting.

- `/home/runner/work/safepath/safepath/engine.py`  
  Routing engine logic:
  - downloads a walkable graph from OpenStreetMap (OSMnx)
  - generates synthetic AQI hazard zones along the trip corridor
  - applies weighted penalties to hazard-exposed edges
  - computes Standard and SafePath routes
  - returns route geometry + metrics

- `/home/runner/work/safepath/safepath/static/index.html`  
  Frontend map UI (Leaflet), place search (Nominatim), route rendering, metrics display.

- `/home/runner/work/safepath/safepath/start.sh`  
  Convenience script to start the API with uvicorn from `.venv`.

---

## How It Works

1. User sets start/end locations in the UI (search or map click).
2. Browser sends `POST /api/route` with coordinates.
3. Backend:
   - validates coordinates
   - fetches a pedestrian graph from OSM
   - creates hazard zones
   - computes two routes:
     - Standard: weight = `length`
     - SafePath: weight = `safepath_weight` (`length * 100` in hazard, else `length`)
4. API returns:
   - `standard_route` polyline
   - `safepath_route` polyline
   - `hazard_zones`
   - `metrics` for both routes
5. UI draws both routes and hazard circles and shows summary metrics.

---

## API

### Health Check

`GET /api/health`

Response:

```json
{
  "status": "ok",
  "service": "SafePath Routing Engine"
}
```

### Route Calculation

`POST /api/route`

Request body:

```json
{
  "start_lat": 28.6129,
  "start_lon": 77.2295,
  "end_lat": 28.6315,
  "end_lon": 77.2167
}
```

Successful response includes:

- `standard_route`: list of `[lat, lon]`
- `safepath_route`: list of `[lat, lon]`
- `hazard_zones`: generated AQI hazard circles
- `metrics.standard` and `metrics.safepath`:
  - `distance_km`
  - `exposure`
  - `hazard_distance_km`

---

## Local Setup

From `/home/runner/work/safepath/safepath`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run server:

```bash
./start.sh
```

Or directly:

```bash
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

- App: `http://localhost:8000/`
- API docs: `http://localhost:8000/docs`

---

## CI/CD Auto-Deploy (GitHub Actions)

This repository includes:

- `/home/runner/work/safepath/safepath/.github/workflows/ci-deploy.yml`

Behavior:

- On `pull_request` to `main`:
  - installs dependencies
  - validates Python syntax
  - optionally triggers preview deploy webhook
- On `push` to `main`:
  - runs the same verification
  - triggers production deploy webhook

Required repository secrets:

- `DEPLOY_WEBHOOK_URL` (required for production auto-deploy)
- `PREVIEW_DEPLOY_WEBHOOK_URL` (optional for PR preview deployments)

If a secret is missing, the related deploy step is skipped safely.

---

## Notes & Constraints

- Routing depends on live OSM/Overpass availability.
- Place search uses Nominatim from the browser.
- Hazard zones are synthetic demo zones, not live AQI data.
- Route requests are constrained by engine validation:
  - minimum trip distance: 20m
  - maximum trip distance: 6km
