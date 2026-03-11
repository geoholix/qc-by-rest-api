import React, { useEffect, useMemo, useRef, useState } from "react";
import Map from "@arcgis/core/Map";
import MapView from "@arcgis/core/views/MapView";
import FeatureLayer from "@arcgis/core/layers/FeatureLayer";
import GeoJSONLayer from "@arcgis/core/layers/GeoJSONLayer";

const API_BASE = "http://localhost:8000";

const ERROR_STYLES = {
  invalid_geometry: "Invalid geometry",
  topology_overlap: "Topology overlap",
  overshoot: "Overshoot / dangle",
  attribute_error: "Attribute error",
};

export default function App() {
  const mapDivRef = useRef(null);
  const mapRef = useRef(null);
  const viewRef = useRef(null);
  const sourceLayerRef = useRef(null);
  const resultLayersRef = useRef([]);

  const [layerUrl, setLayerUrl] = useState("");
  const [requiredFields, setRequiredFields] = useState("NOP,NAMA_WP,LUAS");
  const [tol, setTol] = useState(0);
  const [summary, setSummary] = useState(null);
  const [downloads, setDownloads] = useState({});
  const [status, setStatus] = useState("Ready. Add a layer to start.");
  const [activeTab, setActiveTab] = useState("summary");
  const [isLayerLoaded, setIsLayerLoaded] = useState(false);
  const [log, setLog] = useState(["App loaded. Waiting for layer URL."]);

  const summaryEntries = useMemo(() => Object.entries(summary || {}), [summary]);
  const totalErrors = useMemo(
    () => summaryEntries.reduce((total, [, count]) => total + Number(count || 0), 0),
    [summaryEntries]
  );

  useEffect(() => {
    if (!mapDivRef.current || viewRef.current) return;

    const map = new Map({ basemap: "dark-gray-vector" });
    const view = new MapView({
      container: mapDivRef.current,
      map,
      center: [106.8456, -6.2088],
      zoom: 11,
      ui: { components: ["zoom", "compass"] },
    });

    mapRef.current = map;
    viewRef.current = view;

    return () => {
      if (viewRef.current) viewRef.current.destroy();
      viewRef.current = null;
      mapRef.current = null;
    };
  }, []);

  const addLog = (msg) => setLog((old) => [msg, ...old].slice(0, 50));

  async function addLayer() {
    if (!layerUrl.trim()) {
      setStatus("Please enter layer URL.");
      return;
    }

    setStatus("Loading layer...");
    addLog(`Loading source layer: ${layerUrl}`);

    try {
      if (sourceLayerRef.current && mapRef.current) {
        mapRef.current.remove(sourceLayerRef.current);
      }

      const source = new FeatureLayer({
        url: layerUrl.trim(),
        title: "Input Layer",
        opacity: 0.65,
      });

      await source.load();
      mapRef.current.add(source);
      sourceLayerRef.current = source;

      const extent = source.fullExtent;
      if (extent && viewRef.current) {
        await viewRef.current.goTo(extent.expand(1.2));
      }

      setIsLayerLoaded(true);
      setStatus("Layer loaded. You can run QC scan.");
      addLog("Layer added successfully.");
    } catch (err) {
      setIsLayerLoaded(false);
      setStatus(`Layer failed: ${err?.message || "unknown error"}`);
      addLog(`Layer load failed: ${err?.message || "unknown"}`);
    }
  }

  async function runScan() {
    if (!isLayerLoaded) {
      setStatus("Add layer first before scan.");
      return;
    }

    setStatus("Running QC scan...");
    addLog("Scan started.");

    const resp = await fetch(`${API_BASE}/api/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        layer_url: layerUrl.trim(),
        required_fields: requiredFields
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean),
        line_endpoint_tolerance: Number(tol || 0),
      }),
    });

    const data = await resp.json();
    if (!resp.ok) {
      const errorMsg = data.error || "scan failed";
      setStatus(`Scan failed: ${errorMsg}`);
      addLog(`Scan failed: ${errorMsg}`);
      return;
    }

    setSummary(data.summary || {});
    setDownloads(data.downloads || {});
    setStatus(`Scan completed. Total errors: ${Object.values(data.summary || {}).reduce((a, b) => a + b, 0)}`);
    setActiveTab("summary");
    addLog("Scan completed and exports are ready.");

    if (mapRef.current) {
      resultLayersRef.current.forEach((lyr) => mapRef.current.remove(lyr));
      resultLayersRef.current = [];

      Object.entries(data.downloads || {}).forEach(([name, url]) => {
        if (name === "html_report") return;
        const layer = new GeoJSONLayer({ url: `${API_BASE}${url}`, title: `QC - ${name}` });
        mapRef.current.add(layer);
        resultLayersRef.current.push(layer);
      });
    }
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">✦</div>
          <div>
            <h1>Spatial Data QC</h1>
            <p>ArcGIS REST Service Inspector & Error Reporter</p>
          </div>
        </div>

        <div className="form-block">
          <label>SERVICE URL (MAPSERVER LAYER)</label>
          <input
            value={layerUrl}
            onChange={(e) => setLayerUrl(e.target.value)}
            placeholder="https://.../MapServer/0"
          />

          <div className="grid2">
            <div>
              <label>REQUIRED FIELDS (COMMA-SEPARATED)</label>
              <input
                value={requiredFields}
                onChange={(e) => setRequiredFields(e.target.value)}
                placeholder="NOP, NAMA_WP, LUAS"
              />
            </div>
            <div>
              <label>OVERSHOOT TOLERANCE</label>
              <input type="number" step="0.000001" value={tol} onChange={(e) => setTol(e.target.value)} />
            </div>
          </div>

          <button className="btn btn-outline" onClick={addLayer}>+ Add Layer</button>
          <button className="btn btn-primary" disabled={!isLayerLoaded} onClick={runScan}>▶ Run QC Scan</button>
        </div>

        <div className="tabs">
          <button className={activeTab === "summary" ? "active" : ""} onClick={() => setActiveTab("summary")}>SUMMARY</button>
          <button className={activeTab === "errors" ? "active" : ""} onClick={() => setActiveTab("errors")}>ERRORS</button>
          <button className={activeTab === "log" ? "active" : ""} onClick={() => setActiveTab("log")}>LOG</button>
        </div>

        <div className="tab-content">
          {activeTab === "summary" && (
            <div>
              {!summary && <p>🔎 Run a scan to see results</p>}
              {summary && (
                <>
                  <p className="total">Total errors: {totalErrors}</p>
                  <div className="cards">
                    {summaryEntries.map(([k, v]) => (
                      <div key={k} className="card">
                        <strong>{ERROR_STYLES[k] || k}</strong>
                        <span>{v}</span>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {activeTab === "errors" && (
            <ul className="downloads">
              {Object.entries(downloads).length === 0 && <li>No exports yet.</li>}
              {Object.entries(downloads).map(([k, v]) => (
                <li key={k}>
                  <a href={`${API_BASE}${v}`} target="_blank" rel="noreferrer">
                    Download {k === "html_report" ? "HTML report" : `${k}.geojson`}
                  </a>
                </li>
              ))}
            </ul>
          )}

          {activeTab === "log" && (
            <ul className="log">
              {log.map((line, idx) => (
                <li key={`${line}-${idx}`}>{line}</li>
              ))}
            </ul>
          )}
        </div>

        <div className="status">{status}</div>
      </aside>

      <main className="map-pane">
        <div className="map-message">
          {!isLayerLoaded ? "Enter service URL and click Add Layer" : "Layer loaded. Run QC to highlight errors."}
        </div>
        <div id="viewDiv" ref={mapDivRef} />
      </main>
    </div>
  );
}
