from __future__ import annotations

import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from qc_engine import QCConfig, run_qc

app = Flask(__name__)
CORS(app)
EXPORT_ROOT = Path("backend/exports")
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)

RUNS = {}


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/scan")
def scan():
    payload = request.get_json(force=True)
    layer_url = (payload.get("layer_url") or "").strip()
    required_fields = payload.get("required_fields") or []
    tolerance = float(payload.get("line_endpoint_tolerance", 0.0))

    if not layer_url:
        return jsonify({"error": "layer_url is required"}), 400

    run_id = str(int(time.time() * 1000))
    out_dir = EXPORT_ROOT / run_id

    summary, file_refs = run_qc(
        QCConfig(
            layer_url=layer_url,
            out_dir=out_dir,
            required_fields=required_fields,
            line_endpoint_tolerance=tolerance,
        )
    )

    RUNS[run_id] = {"out_dir": str(out_dir), "summary": summary}

    downloads = {
        name: f"/api/export/{run_id}/{name}" for name in file_refs.keys()
    }

    return jsonify({"run_id": run_id, "summary": summary, "downloads": downloads})


@app.get("/api/export/<run_id>/<name>")
def export_file(run_id: str, name: str):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id not found"}), 404

    out_dir = Path(run["out_dir"])
    if name == "html_report":
        target = out_dir / "qc_report.html"
        fname = f"qc_report_{run_id}.html"
    else:
        target = out_dir / f"{name}.geojson"
        fname = f"{name}_{run_id}.geojson"

    if not target.exists():
        return jsonify({"error": "file not found"}), 404

    return send_file(target, as_attachment=True, download_name=fname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
