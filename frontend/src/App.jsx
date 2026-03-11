import React, { useMemo, useState } from "react";
import Map from "@arcgis/core/Map";
import MapView from "@arcgis/core/views/MapView";
import GeoJSONLayer from "@arcgis/core/layers/GeoJSONLayer";

const API_BASE = "http://localhost:8000";

export default function App() {
  const [layerUrl, setLayerUrl] = useState("");
  const [requiredFields, setRequiredFields] = useState("NOP,KECAMATAN");
  const [tol, setTol] = useState(0);
  const [summary, setSummary] = useState(null);
  const [downloads, setDownloads] = useState({});
  const [status, setStatus] = useState("Idle");

  const summaryEntries = useMemo(() => Object.entries(summary || {}), [summary]);

  async function runScan() {
    setStatus("Scanning...");
    const resp = await fetch(`${API_BASE}/api/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        layer_url: layerUrl,
        required_fields: requiredFields.split(",").map((v) => v.trim()).filter(Boolean),
        line_endpoint_tolerance: Number(tol || 0),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      setStatus(`Failed: ${data.error || "unknown"}`);
      return;
    }
    setSummary(data.summary);
    setDownloads(data.downloads || {});
    setStatus("Done");

    if (!window.__qcMapInit) {
      window.__qcMapInit = true;
      const map = new Map({ basemap: "gray-vector" });
      const view = new MapView({ container: "viewDiv", map, center: [106.8456, -6.2088], zoom: 11 });
      Object.entries(data.downloads || {}).forEach(([name, url]) => {
        if (name === "html_report") return;
        map.add(new GeoJSONLayer({ url: `${API_BASE}${url}`, title: name }));
      });
      window.__view = view;
    }
  }

  return (
    <div className="app">
      <h1>Spatial QC Dashboard (React + ArcGIS JS API)</h1>
      <div className="controls">
        <input placeholder="ArcGIS layer URL" value={layerUrl} onChange={(e) => setLayerUrl(e.target.value)} />
        <input placeholder="Required fields comma separated" value={requiredFields} onChange={(e) => setRequiredFields(e.target.value)} />
        <input type="number" step="0.000001" value={tol} onChange={(e) => setTol(e.target.value)} />
        <button onClick={runScan}>Scan</button>
      </div>

      <p>Status: {status}</p>

      <div className="cards">
        {summaryEntries.map(([k, v]) => (
          <div key={k} className="card">
            <strong>{k}</strong>
            <div>{v} error(s)</div>
          </div>
        ))}
      </div>

      <h3>Downloads</h3>
      <ul>
        {Object.entries(downloads).map(([k, v]) => (
          <li key={k}>
            <a href={`${API_BASE}${v}`} target="_blank" rel="noreferrer">
              Download {k}
            </a>
          </li>
        ))}
      </ul>

      <div id="viewDiv" className="map" />
    </div>
  );
}
