from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import geopandas as gpd
import requests
from arcgis.features import FeatureLayer
from shapely.geometry import LineString, Point, shape


class QCServiceError(RuntimeError):
    """Raised when layer metadata or features cannot be read safely."""


@dataclass
class QCConfig:
    layer_url: str
    out_dir: Path
    required_fields: List[str]
    line_endpoint_tolerance: float = 0.0
    batch_size: int = 1000
    timeout_seconds: int = 90


def _chunked(values: List[int], size: int) -> Iterable[List[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class ArcGISQCScanner:
    """Layer reader with ArcGIS Python API first, REST fallback second."""

    def __init__(self, layer_url: str, timeout_seconds: int = 90):
        self.layer_url = layer_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.layer = FeatureLayer(self.layer_url)
        self.session = requests.Session()

    def _rest_get(self, endpoint: str, params: Dict) -> Dict:
        response = self.session.get(endpoint, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            message = payload["error"].get("message", "ArcGIS REST error")
            details = payload["error"].get("details", [])
            raise QCServiceError(f"{message}. {' | '.join(details)}")
        return payload

    def metadata(self) -> Dict:
        try:
            return dict(self.layer.properties)
        except Exception:
            return self._rest_get(self.layer_url, {"f": "json"})

    def _load_via_arcgis_api(self) -> gpd.GeoDataFrame:
        fs = self.layer.query(where="1=1", out_fields="*", return_geometry=True)
        if not fs or len(fs.features) == 0:
            return gpd.GeoDataFrame(columns=["_oid", "geometry"], geometry="geometry", crs="EPSG:4326")

        sdf = fs.sdf
        if "SHAPE" not in sdf.columns:
            raise QCServiceError("Feature layer does not contain SHAPE geometry column.")

        return gpd.GeoDataFrame(sdf.drop(columns=["SHAPE"]).copy(), geometry=sdf["SHAPE"], crs="EPSG:4326")

    def _load_via_rest(self, batch_size: int) -> gpd.GeoDataFrame:
        ids_payload = self._rest_get(
            f"{self.layer_url}/query",
            {"f": "json", "where": "1=1", "returnIdsOnly": "true"},
        )
        object_ids = ids_payload.get("objectIds") or []
        if not object_ids:
            return gpd.GeoDataFrame(columns=["_oid", "geometry"], geometry="geometry", crs="EPSG:4326")

        records: List[Dict] = []
        for chunk in _chunked(object_ids, batch_size):
            payload = self._rest_get(
                f"{self.layer_url}/query",
                {
                    "f": "geojson",
                    "objectIds": ",".join(str(v) for v in chunk),
                    "outFields": "*",
                    "outSR": "4326",
                },
            )
            for feat in payload.get("features", []):
                row = dict(feat.get("properties") or {})
                geom = feat.get("geometry")
                row["geometry"] = shape(geom) if geom else None
                records.append(row)

        return gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")

    def load_gdf(self, batch_size: int) -> gpd.GeoDataFrame:
        # ArcGIS API may fail on some enterprise auth handshakes (e.g. token key missing).
        # Fallback to plain REST request path to keep scan working for public services.
        try:
            gdf = self._load_via_arcgis_api()
        except Exception:
            gdf = self._load_via_rest(batch_size=batch_size)

        oid_col = next((c for c in ["OBJECTID", "objectid", "FID", "fid"] if c in gdf.columns), None)
        if oid_col:
            gdf = gdf.rename(columns={oid_col: "_oid"})
        elif "_oid" not in gdf.columns:
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
                hits.append(
                    {
                        "_oid": polygons.iloc[i]["_oid"],
                        "_oid_other": polygons.iloc[j]["_oid"],
                        "error_type": "topology_overlap",
                        "error_detail": "polygon overlap area > 0",
                        "geometry": inter,
                    }
                )
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
            issues.append(
                {
                    "_oid": ep["_oid"],
                    "error_type": "overshoot_or_dangle",
                    "error_detail": f"endpoint has no connection within tolerance={tolerance}",
                    "geometry": ep["geometry"],
                }
            )
    return gpd.GeoDataFrame(issues, geometry="geometry", crs=gdf.crs)


def detect_attribute_errors(gdf: gpd.GeoDataFrame, required_fields: List[str]) -> gpd.GeoDataFrame:
    rows = []
    for field in required_fields:
        if field not in gdf.columns:
            for rec in gdf.itertuples():
                rows.append(
                    {
                        "_oid": rec._oid,
                        "error_type": "attribute_missing_field",
                        "error_detail": f"required field '{field}' not present",
                        "geometry": rec.geometry,
                    }
                )
            continue

        for rec in gdf.itertuples():
            val = getattr(rec, field)
            empty = val is None
            if isinstance(val, str):
                empty = empty or val.strip() == ""
            if isinstance(val, float):
                empty = empty or math.isnan(val)
            if empty:
                rows.append(
                    {
                        "_oid": rec._oid,
                        "error_type": "attribute_null_or_empty",
                        "error_detail": f"required field '{field}' is empty",
                        "geometry": rec.geometry,
                    }
                )
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
    scanner = ArcGISQCScanner(config.layer_url, timeout_seconds=config.timeout_seconds)
    gdf = scanner.load_gdf(batch_size=config.batch_size)
    if gdf.empty:
        raise QCServiceError("Layer has no records or cannot be queried.")

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
