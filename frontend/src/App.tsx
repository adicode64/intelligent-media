import { useCallback, useEffect, useRef, useState } from "react";
import {
  API_BASE,
  getHealth,
  getResults,
  uploadImage,
  type AnalysisResponse,
} from "./api";

const TERMINAL = new Set(["completed", "failed"]);

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalysisResponse | null>(null);
  const [health, setHealth] = useState<string>("checking…");
  const inputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    getHealth()
      .then((h) => setHealth(`api ok · v${h.version}`))
      .catch(() => setHealth("api unreachable"));
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  const pick = useCallback((f: File | null) => {
    setResult(null);
    setError(null);
    setFile(f);
    setPreviewUrl((old) => {
      if (old) URL.revokeObjectURL(old);
      return f ? URL.createObjectURL(f) : null;
    });
  }, []);

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const { id } = await uploadImage(file);
      // Poll the single results URL — it returns the current status while pending.
      const tick = async () => {
        try {
          const r = await getResults(id);
          setResult(r);
          if (TERMINAL.has(r.status)) {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            setBusy(false);
          }
        } catch (e) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setBusy(false);
          setError((e as Error).message);
        }
      };
      await tick();
      if (!pollRef.current) pollRef.current = window.setInterval(tick, 1500);
    } catch (e) {
      setBusy(false);
      setError((e as Error).message);
    }
  }

  const s = result?.summary;

  return (
    <div className="wrap">
      <header className="hero">
        <div className="badge">{health}</div>
        <h1>Intelligent Media Processing Pipeline</h1>
        <p>
          Upload a vehicle image. It is stored, queued to Celery, and analysed for blur,
          brightness, dimensions, duplicates, screenshot artefacts and plate text. Every
          verdict is an explicit heuristic with a confidence value — not a guarantee.
        </p>
      </header>

      <section className="card">
        <div
          className={`drop${over ? " over" : ""}`}
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            pick(e.dataTransfer.files?.[0] ?? null);
          }}
        >
          <strong>{file ? file.name : "Drop an image or click to browse"}</strong>
          <span>JPEG · PNG · WEBP · BMP — up to 10 MB</span>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => pick(e.target.files?.[0] ?? null)}
        />

        {file && previewUrl && (
          <div className="preview">
            <img src={previewUrl} alt={`Preview of ${file.name}`} />
            <div className="meta">
              {file.type || "unknown type"} · {(file.size / 1024).toFixed(0)} KB
              <br />
              endpoint: {API_BASE}/api/v1/images/upload
            </div>
          </div>
        )}

        <div className="row">
          <button onClick={submit} disabled={!file || busy}>
            {busy ? "Analysing…" : "Upload & analyse"}
          </button>
          <button className="ghost" onClick={() => pick(null)} disabled={busy || !file}>
            Clear
          </button>
        </div>

        {error && <div className="err">{error}</div>}
      </section>

      {result && (
        <section className="card">
          <div className="row" style={{ marginTop: 0, justifyContent: "space-between" }}>
            <div className="meta">image_id: {result.image_id}</div>
            <span className={`pill s-${result.status}`}>{result.status}</span>
          </div>

          {result.status !== "completed" && (
            <p className="meta" style={{ marginTop: 10 }}>
              {result.message ?? "Waiting for the worker to finish…"}
            </p>
          )}

          {s && (
            <>
              <div className="summary">
                <div className="stat">
                  <b className={`s-${s.overall_status}`}>{s.overall_status}</b>
                  <span>overall</span>
                </div>
                <div className="stat">
                  <b>{(s.confidence * 100).toFixed(0)}%</b>
                  <span>confidence</span>
                </div>
                <div className="stat">
                  <b className="s-pass">{s.passed}</b>
                  <span>passed</span>
                </div>
                <div className="stat">
                  <b className="s-warning">{s.warnings}</b>
                  <span>warnings</span>
                </div>
                <div className="stat">
                  <b className="s-fail">{s.failures}</b>
                  <span>failures</span>
                </div>
                <div className="stat">
                  <b className="s-uncertain">{s.uncertain}</b>
                  <span>uncertain</span>
                </div>
              </div>
              {s.notes.length > 0 && (
                <ul className="notes">
                  {s.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              )}
            </>
          )}

          <div className="meta" style={{ marginTop: 14 }}>
            vehicle number: <strong>{result.vehicle_number ?? "not detected"}</strong>
          </div>

          {result.checks.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Check</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Conf.</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {result.checks.map((c) => (
                  <tr key={c.name}>
                    <td>{c.name}</td>
                    <td>
                      <span className={`pill s-${c.status}`}>{c.status}</span>
                    </td>
                    <td>{fmt(c.score)}</td>
                    <td>{(c.confidence * 100).toFixed(0)}%</td>
                    <td>{c.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <footer>
        React + Vite client. Configure the API host with <code>VITE_API_BASE_URL</code>.
      </footer>
    </div>
  );
}
