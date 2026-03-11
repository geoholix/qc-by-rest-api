# Spatial QC App (Backend + Frontend)

Refined project structure:

- `backend/` → Python API + QC engine (ArcGIS Python API)
- `frontend/` → React dashboard + ArcGIS JavaScript API map

## Features

1. User inputs ArcGIS REST layer URL and clicks **Add Layer** to preview it on map.
2. After layer loads, user clicks **Run QC Scan**.
3. Backend scans all data and checks:
   - invalid geometry
   - topology overlap
   - overshoot/dangle
   - missing/empty required attributes
4. Dashboard shows summary cards (`2 topology_overlap`, `3 overshoot`, etc).
5. User can export each error category as **GeoJSON**.
6. App also generates **HTML report** (`qc_report.html`) listing all detected errors.

---

## Backend (ArcGIS Python API)

### Run backend
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

API endpoints:
- `POST /api/scan`
- `GET /api/export/<run_id>/<name>`

`name` can be:
- `invalid_geometry`
- `topology_overlap`
- `overshoot`
- `attribute_error`
- `html_report`

---

## Frontend (React + ArcGIS JavaScript API)

### Run frontend
```bash
cd frontend
npm install
npm run dev
```

Open Vite URL (usually `http://localhost:5173`).

> Frontend expects backend at `http://localhost:8000`.

---


## Reliability notes

- Backend now tries ArcGIS Python API query first, then falls back to direct ArcGIS REST query (`/query?f=geojson`) when enterprise auth negotiation fails (for example `KeyError: token` from NTLM hooks).
- Scan endpoint returns structured JSON errors instead of Flask traceback pages:
  - `422` for layer/service query problems
  - `400` for invalid request payload

---

## Example layer URL

`https://petapajak.jakarta.go.id/arcgis/rest/services/analytics/PETA_BIDANG_PBB_P2_DKI_JAKARTASATU/MapServer/0`
