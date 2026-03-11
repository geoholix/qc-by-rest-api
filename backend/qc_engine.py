from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import geopandas as gpd
from arcgis.features import FeatureLayer
from shapely.geometry import LineString, Point


@dataclass
class QCConfig:
    layer_url: str
    out_dir: Path
    required_fields: List[str]
    line_endpoint_tolerance: float = 0.0


class ArcGISQCScanner:
    def __init__(self, layer_url: str):
        self.layer = FeatureLayer(layer_url)

    def metadata(self) -> Dict:
        return dict(self.layer.properties)

    def load_gdf(self) -> gpd.GeoDataFrame:
        fs = self.layer.query(where="1=1", out_fields="*", return_geometry=True)
        if not fs or len(fs.features) == 0:
            return gpd.GeoDataFrame(columns=["_oid", "geometry"], geometry="geometry", crs="EPSG:4326")

        sdf = fs.sdf
        if "SHAPE" not in sdf.columns:
            raise RuntimeError("Layer does not contain SHAPE geometry column.")

        gdf = gpd.GeoDataFrame(sdf.drop(columns=["SHAPE"]).copy(), geometry=sdf["SHAPE"], crs="EPSG:4326")
        oid_col = next((c for c in ["OBJECTID", "objectid", "FID", "fid"] if c in gdf.columns), None)
        if oid_col:
            gdf = gdf.rename(columns={oid_col: "_oid"})
        else:
            gdf["_oid"] = range(1, len(gdf) + 1)
        return gdf


def infer_required_fields(metadata: Dict) -> List[str]:
    required = []
    for field in metadata.get("fields", []):
        ftype = field.get("type")
        if ftype in ("esriFieldTypeOID", "esriFieldTypeGeometry"):
            continue
        if field.get("nullable") is False and field.get("name"):
            required.append(field["name"])
    return required


def detect_invalid_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rows = gdf[gdf.geometry.isna() | ~gdf.geometry.is_valid].copy()
    rows["error_type"] = "invalid_geometry"
    rows["error_detail"] = rows.geometry.apply(lambda g: "missing geometry" if g is None else "invalid geometry")
    return rows


def detect_polygon_overlaps(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    polygons = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy().reset_index(drop=True)
    if polygons.empty:
        return gpd.GeoDataFrame(columns=["_oid", "_oid_other", "error_type", "error_detail", "geometry"], geometry="geometry", crs=gdf.crs)

    sindex = polygons.sindex
    hits = []
    for i, geom in enumerate(polygons.geometry):
        if geom is None:
            continue
        for j in sindex.intersection(geom.bounds):
            if j <= i:
                continue
            other = polygons.geometry.iloc[j]
            if other is None or not geom.intersects(other):
                continue
            inter = geom.intersection(other)
            if not inter.is_empty and inter.area > 0:
                hits.append({
                    "_oid": polygons.iloc[i]["_oid"],
                    "_oid_other": polygons.iloc[j]["_oid"],
                    "error_type": "topology_overlap",
                    "error_detail": "polygon overlap area > 0",
                    "geometry": inter,
                })
    return gpd.GeoDataFrame(hits, geometry="geometry", crs=gdf.crs)


def _endpoints(geom) -> List[Point]:
    if isinstance(geom, LineString):
        coords = list(geom.coords)
        return [Point(coords[0]), Point(coords[-1])] if coords else []
    if geom is not None and geom.geom_type == "MultiLineString":
        pts: List[Point] = []
        for seg in geom.geoms:
            coords = list(seg.coords)
            if coords:
                pts.extend([Point(coords[0]), Point(coords[-1])])
        return pts
    return []


def detect_overshoots(gdf: gpd.GeoDataFrame, tolerance: float) -> gpd.GeoDataFrame:
    lines = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy().reset_index(drop=True)
    if lines.empty:
        return gpd.GeoDataFrame(columns=["_oid", "error_type", "error_detail", "geometry"], geometry="geometry", crs=gdf.crs)

    endpoints = []
    for i, geom in enumerate(lines.geometry):
        for pt in _endpoints(geom):
            endpoints.append({"_oid": lines.iloc[i]["_oid"], "line_idx": i, "geometry": pt})

    sindex = lines.sindex
    issues = []
    for ep in endpoints:
        nearby = list(sindex.intersection(ep["geometry"].buffer(tolerance).bounds))
        connected = False
        for idx in nearby:
            d = ep["geometry"].distance(lines.geometry.iloc[idx])
            if d <= tolerance and (idx != ep["line_idx"] or d == 0):
                connected = True
                break
        if not connected:
            issues.append({
                "_oid": ep["_oid"],
                "error_type": "overshoot_or_dangle",
                "error_detail": f"endpoint has no connection within tolerance={tolerance}",
                "geometry": ep["geometry"],
            })
    return gpd.GeoDataFrame(issues, geometry="geometry", crs=gdf.crs)


def detect_attribute_errors(gdf: gpd.GeoDataFrame, required_fields: List[str]) -> gpd.GeoDataFrame:
    rows = []
    for field in required_fields:
        if field not in gdf.columns:
            for rec in gdf.itertuples():
                rows.append({
                    "_oid": rec._oid,
                    "error_type": "attribute_missing_field",
                    "error_detail": f"required field '{field}' not present",
                    "geometry": rec.geometry,
                })
            continue

        for rec in gdf.itertuples():
            val = getattr(rec, field)
            empty = val is None
            if isinstance(val, str):
                empty = empty or val.strip() == ""
            if isinstance(val, float):
                empty = empty or math.isnan(val)
            if empty:
                rows.append({
                    "_oid": rec._oid,
                    "error_type": "attribute_null_or_empty",
                    "error_detail": f"required field '{field}' is empty",
                    "geometry": rec.geometry,
                })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs)


def write_report_html(out_dir: Path, layer_url: str, summary: Dict[str, int], details: Dict[str, gpd.GeoDataFrame]) -> Path:
    rows = []
    for key, gdf in details.items():
        if gdf.empty:
            continue
        for rec in gdf[["_oid", "error_type", "error_detail"]].itertuples(index=False):
            rows.append(f"<tr><td>{key}</td><td>{rec._oid}</td><td>{rec.error_type}</td><td>{rec.error_detail}</td></tr>")

    html = f"""
<!doctype html>
<html><head><meta charset='utf-8'><title>QC Report</title>
<style>body{{font-family:Arial;margin:20px}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccc;padding:6px}}</style>
</head><body>
<h1>QC Report</h1>
<p><b>Layer URL:</b> {layer_url}</p>
<h2>Summary</h2>
<ul>
{''.join(f'<li>{k}: {v}</li>' for k, v in summary.items())}
</ul>
<h2>Error List</h2>
<table><thead><tr><th>Category</th><th>OID</th><th>Error Type</th><th>Detail</th></tr></thead><tbody>
{''.join(rows) if rows else '<tr><td colspan="4">No errors found</td></tr>'}
</tbody></table>
</body></html>
"""
    report_path = out_dir / "qc_report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path


def run_qc(config: QCConfig) -> Tuple[Dict[str, int], Dict[str, str]]:
    scanner = ArcGISQCScanner(config.layer_url)
    gdf = scanner.load_gdf()
    if gdf.empty:
        raise RuntimeError("Layer has no records.")

    required_fields = list(dict.fromkeys(config.required_fields + infer_required_fields(scanner.metadata())))

    outputs = {
        "invalid_geometry": detect_invalid_geometries(gdf),
        "topology_overlap": detect_polygon_overlaps(gdf),
        "overshoot": detect_overshoots(gdf, config.line_endpoint_tolerance),
        "attribute_error": detect_attribute_errors(gdf, required_fields),
    }

    config.out_dir.mkdir(parents=True, exist_ok=True)
    file_refs: Dict[str, str] = {}
    for name, df in outputs.items():
        if df.empty:
            continue
        fpath = config.out_dir / f"{name}.geojson"
        df.to_file(fpath, driver="GeoJSON")
        file_refs[name] = str(fpath)

    summary = {name: int(len(df)) for name, df in outputs.items()}
    (config.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report_path = write_report_html(config.out_dir, config.layer_url, summary, outputs)
    file_refs["html_report"] = str(report_path)

    return summary, file_refs
