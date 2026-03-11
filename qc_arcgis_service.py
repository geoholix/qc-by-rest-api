#!/usr/bin/env python3
"""QC ArcGIS REST layer data for geometry and attribute issues."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, Point, shape


@dataclass
class QCConfig:
    layer_url: str
    out_dir: pathlib.Path
    required_fields: List[str]
    batch_size: int
    timeout: int
    line_endpoint_tolerance: float
    export_shapefile: bool


class ArcGISLayerClient:
    def __init__(self, layer_url: str, timeout: int = 60):
        self.layer_url = layer_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Dict) -> Dict:
        response = self.session.get(endpoint, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS REST error: {payload['error']}")
        return payload

    def get_layer_metadata(self) -> Dict:
        return self._get(self.layer_url, {"f": "json"})

    def get_object_ids(self) -> List[int]:
        payload = self._get(
            f"{self.layer_url}/query",
            {
                "f": "json",
                "where": "1=1",
                "returnIdsOnly": "true",
            },
        )
        return payload.get("objectIds", [])

    def fetch_features_geojson(self, object_ids: Iterable[int], out_fields: str = "*") -> Dict:
        ids = ",".join(str(v) for v in object_ids)
        return self._get(
            f"{self.layer_url}/query",
            {
                "f": "geojson",
                "objectIds": ids,
                "outFields": out_fields,
                "outSR": "4326",
            },
        )


def chunked(values: List[int], n: int) -> Iterable[List[int]]:
    for i in range(0, len(values), n):
        yield values[i : i + n]


def load_layer_as_gdf(client: ArcGISLayerClient, batch_size: int) -> gpd.GeoDataFrame:
    object_ids = client.get_object_ids()
    if not object_ids:
        return gpd.GeoDataFrame(columns=["_oid", "geometry"], geometry="geometry", crs="EPSG:4326")

    records: List[Dict] = []
    for oid_group in chunked(object_ids, batch_size):
        data = client.fetch_features_geojson(oid_group)
        for feat in data.get("features", []):
            properties = feat.get("properties", {})
            geom = feat.get("geometry")
            if geom is None:
                geometry = None
            else:
                geometry = shape(geom)
            row = {**properties, "geometry": geometry}
            records.append(row)

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")

    oid_candidates = [c for c in ["OBJECTID", "objectid", "fid", "FID"] if c in gdf.columns]
    if oid_candidates:
        gdf = gdf.rename(columns={oid_candidates[0]: "_oid"})
    elif "_oid" not in gdf.columns:
        gdf["_oid"] = range(1, len(gdf) + 1)

    return gdf


def detect_invalid_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    invalid = gdf[gdf.geometry.isna() | ~gdf.geometry.is_valid].copy()
    invalid["error_type"] = "invalid_geometry"
    invalid["error_detail"] = invalid.geometry.apply(
        lambda g: "missing geometry" if g is None else "invalid geometry"
    )
    return invalid


def detect_polygon_overlaps(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    polygons = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if polygons.empty:
        return polygons.iloc[0:0].assign(error_type=pd.Series(dtype=str), error_detail=pd.Series(dtype=str))

    polygons = polygons.reset_index(drop=True)
    sindex = polygons.sindex
    overlap_rows: List[Dict] = []
    for idx, geom in enumerate(polygons.geometry):
        if geom is None:
            continue
        for candidate in sindex.intersection(geom.bounds):
            if candidate <= idx:
                continue
            other = polygons.geometry.iloc[candidate]
            if other is None:
                continue
            if not geom.intersects(other):
                continue
            inter = geom.intersection(other)
            if inter.is_empty:
                continue
            if inter.area > 0:
                overlap_rows.append(
                    {
                        "_oid": polygons.iloc[idx]["_oid"],
                        "_oid_other": polygons.iloc[candidate]["_oid"],
                        "geometry": inter,
                        "error_type": "overlap",
                        "error_detail": "polygon overlap area > 0",
                    }
                )

    return gpd.GeoDataFrame(overlap_rows, geometry="geometry", crs=gdf.crs)


def _line_endpoints(line_geom) -> List[Point]:
    if isinstance(line_geom, LineString):
        coords = list(line_geom.coords)
        return [Point(coords[0]), Point(coords[-1])] if coords else []
    if line_geom.geom_type == "MultiLineString":
        pts: List[Point] = []
        for seg in line_geom.geoms:
            coords = list(seg.coords)
            if coords:
                pts.extend([Point(coords[0]), Point(coords[-1])])
        return pts
    return []


def detect_line_overshoots(gdf: gpd.GeoDataFrame, tolerance: float) -> gpd.GeoDataFrame:
    lines = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    if lines.empty:
        return lines.iloc[0:0].assign(error_type=pd.Series(dtype=str), error_detail=pd.Series(dtype=str))

    lines = lines.reset_index(drop=True)
    endpoints: List[Dict] = []
    for idx, geom in enumerate(lines.geometry):
        for point in _line_endpoints(geom):
            endpoints.append({"line_idx": idx, "_oid": lines.iloc[idx]["_oid"], "geometry": point})

    if not endpoints:
        return gpd.GeoDataFrame(columns=["_oid", "error_type", "error_detail", "geometry"], geometry="geometry", crs=gdf.crs)

    ep_gdf = gpd.GeoDataFrame(endpoints, geometry="geometry", crs=gdf.crs)
    sindex = lines.sindex
    errors = []

    for row in ep_gdf.itertuples():
        nearby = list(sindex.intersection(row.geometry.buffer(tolerance).bounds))
        connected = False
        for line_idx in nearby:
            line_geom = lines.geometry.iloc[line_idx]
            if line_geom is None:
                continue
            distance = row.geometry.distance(line_geom)
            if distance <= tolerance and (line_idx != row.line_idx or distance == 0):
                connected = True
                break
        if not connected:
            errors.append(
                {
                    "_oid": row._oid,
                    "geometry": row.geometry,
                    "error_type": "overshoot_or_dangle",
                    "error_detail": f"line endpoint has no connection within tolerance={tolerance}",
                }
            )

    return gpd.GeoDataFrame(errors, geometry="geometry", crs=gdf.crs)


def detect_attribute_errors(gdf: gpd.GeoDataFrame, required_fields: List[str]) -> gpd.GeoDataFrame:
    if not required_fields:
        return gpd.GeoDataFrame(columns=["_oid", "error_type", "error_detail", "geometry"], geometry="geometry", crs=gdf.crs)

    issues: List[Dict] = []
    for field in required_fields:
        if field not in gdf.columns:
            for row in gdf.itertuples():
                issues.append(
                    {
                        "_oid": row._oid,
                        "geometry": row.geometry,
                        "error_type": "attribute_missing_field",
                        "error_detail": f"required field '{field}' is absent",
                    }
                )
            continue

        for row in gdf.itertuples():
            val = getattr(row, field)
            empty = val is None
            if isinstance(val, str):
                empty = empty or val.strip() == ""
            if isinstance(val, float):
                empty = empty or math.isnan(val)
            if empty:
                issues.append(
                    {
                        "_oid": row._oid,
                        "geometry": row.geometry,
                        "error_type": "attribute_null_or_empty",
                        "error_detail": f"required field '{field}' is empty",
                    }
                )

    return gpd.GeoDataFrame(issues, geometry="geometry", crs=gdf.crs)


def write_outputs(out_dir: pathlib.Path, outputs: Dict[str, gpd.GeoDataFrame], export_shp: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, gdf in outputs.items():
        if gdf.empty:
            continue

        geojson_path = out_dir / f"{name}.geojson"
        gdf.to_file(geojson_path, driver="GeoJSON")

        if export_shp:
            shp_dir = out_dir / f"{name}_shp"
            shp_dir.mkdir(parents=True, exist_ok=True)
            shp_path = shp_dir / f"{name}.shp"
            gdf.to_file(shp_path, driver="ESRI Shapefile")

    summary = {
        name: int(len(gdf))
        for name, gdf in outputs.items()
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def infer_required_fields(metadata: Dict) -> List[str]:
    required = []
    for field in metadata.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") in ("esriFieldTypeOID", "esriFieldTypeGeometry"):
            continue
        if field.get("nullable") is False:
            required.append(name)
    return required


def run_qc(config: QCConfig) -> Tuple[Dict[str, int], pathlib.Path]:
    client = ArcGISLayerClient(config.layer_url, timeout=config.timeout)
    metadata = client.get_layer_metadata()

    gdf = load_layer_as_gdf(client, config.batch_size)
    if gdf.empty:
        raise RuntimeError("Layer has no records.")

    required_fields = list(dict.fromkeys(config.required_fields + infer_required_fields(metadata)))

    results = {
        "invalid_geometry": detect_invalid_geometries(gdf),
        "overlap": detect_polygon_overlaps(gdf),
        "overshoot": detect_line_overshoots(gdf, config.line_endpoint_tolerance),
        "attribute_error": detect_attribute_errors(gdf, required_fields),
    }
    write_outputs(config.out_dir, results, config.export_shapefile)
    summary = {name: len(df) for name, df in results.items()}
    return summary, config.out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QC ArcGIS Enterprise REST layer for geometry and attribute issues.")
    parser.add_argument("layer_url", help="ArcGIS REST layer URL (feature layer or map layer endpoint)")
    parser.add_argument("--out-dir", default="qc_output", help="Output folder for reports")
    parser.add_argument(
        "--required-field",
        action="append",
        default=[],
        help="Required field to validate (repeat this option to add multiple fields)",
    )
    parser.add_argument("--batch-size", type=int, default=1000, help="Object ID batch size per query")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout (seconds)")
    parser.add_argument(
        "--line-endpoint-tolerance",
        type=float,
        default=0.0,
        help="Tolerance for line endpoint connectivity checks (in layer units)",
    )
    parser.add_argument("--export-shapefile", action="store_true", help="Also export each error layer to Shapefile")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = QCConfig(
        layer_url=args.layer_url,
        out_dir=pathlib.Path(args.out_dir),
        required_fields=args.required_field,
        batch_size=args.batch_size,
        timeout=args.timeout,
        line_endpoint_tolerance=args.line_endpoint_tolerance,
        export_shapefile=args.export_shapefile,
    )

    summary, out_dir = run_qc(config)
    print("QC completed.")
    print(json.dumps(summary, indent=2))
    print(f"Reports written to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
