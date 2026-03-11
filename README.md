# Spatial QC App (Backend + Frontend)

Refined project structure:

- `backend/` → Python API + QC engine (ArcGIS Python API)
- `frontend/` → React dashboard + ArcGIS JavaScript API map

## Features

1. User inputs ArcGIS REST layer URL, clicks **Scan**.
2. Backend scans all data and checks:
   - invalid geometry
   - topology overlap
   - overshoot/dangle
   - missing/empty required attributes
3. Dashboard shows summary cards (`2 topology_overlap`, `3 overshoot`, etc).
4. User can export each error category as **GeoJSON**.
5. App also generates **HTML report** (`qc_report.html`) listing all detected errors.

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

## Example layer URL

`https://petapajak.jakarta.go.id/arcgis/rest/services/analytics/PETA_BIDANG_PBB_P2_DKI_JAKARTASATU/MapServer/0`
# ArcGIS REST Spatial QC Script

This repository provides a Python CLI script to run **spatial and attribute quality control (QC)** directly from an ArcGIS Enterprise REST layer URL.

## What it checks

1. **Geometry errors**
   - Invalid or missing geometry
   - Polygon overlaps
   - Line overshoot/dangle endpoints (connectivity check)
2. **Attribute errors**
   - Empty/null values in required fields
   - Missing required fields

It exports errors so you can share them with other teams for correction.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python qc_arcgis_service.py \
  "https://yourdomain.com/arcgis/rest/services/servicename/MapServer/0" \
  --out-dir reports/pbb_qc \
  --required-field NOP \
  --required-field KECAMATAN \
  --line-endpoint-tolerance 0.00001 \
  --export-shapefile
```

## Outputs

Inside `--out-dir`:

- `invalid_geometry.geojson`
- `overlap.geojson`
- `overshoot.geojson`
- `attribute_error.geojson`
- `<name>_shp/<name>.shp` (if `--export-shapefile` is set)
- `summary.json`

## Notes

- Script queries all records by object IDs in batches.
- Required fields are automatically inferred from ArcGIS fields marked `nullable=false`.
- For `MapServer` layers, ensure querying is enabled.
