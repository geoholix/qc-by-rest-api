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
  "https://petapajak.jakarta.go.id/arcgis/rest/services/analytics/PETA_BIDANG_PBB_P2_DKI_JAKARTASATU/MapServer/0" \
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
