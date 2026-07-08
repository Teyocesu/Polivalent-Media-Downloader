import { useEffect, useMemo, useState } from "react";
import {
  cancelJob,
  downloadFile,
  getInfo,
  getJob,
  login,
  startDownload,
} from "./api.js";

const TOKEN_KEY = "private-media-downloader-token";
const qualities = [
  { value: "best", label: "Mejor compatible" },
  { value: "1080", label: "1080p" },
  { value: "720", label: "720p" },
  { value: "480", label: "480p" },
  { value: "mp3", label: "Solo audio MP3" },
];

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [password, setPassword] = useState("");
  const [url, setUrl] = useState("");
  const [quality, setQuality] = useState("best");
  const [viewState, setViewState] = useState(token ? "idle" : "loggedOut");
  const [metadata, setMetadata] = useState(null);
  const [job, setJob] = useState(null);
  const [jobId, setJobId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const isBusy = useMemo(
    () => ["analyzing", "downloading"].includes(viewState) || isSaving,
    [viewState, isSaving],
  );

  useEffect(() => {
    if (viewState !== "downloading" || !jobId || !token) {
      return undefined;
    }

    let cancelled = false;
    const poll = async () => {
      try {
        const nextJob = await getJob(jobId, token);
        if (cancelled) {
          return;
        }
        setJob(nextJob);
        if (nextJob.status === "done") {
          setViewState("done");
        } else if (nextJob.status === "error" || nextJob.status === "expired") {
          setError(nextJob.message || "La descarga no esta disponible.");
          setViewState("error");
        }
      } catch (err) {
        if (!cancelled) {
          handleApiError(err);
        }
      }
    };

    poll();
    const intervalId = window.setInterval(poll, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [jobId, token, viewState]);

  async function handleLogin(event) {
    event.preventDefault();
    setError("");
    setNotice("");
    try {
      const result = await login(password);
      localStorage.setItem(TOKEN_KEY, result.token);
      setToken(result.token);
      setPassword("");
      setViewState("idle");
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleAnalyze(event) {
    event.preventDefault();
    setError("");
    setNotice("");
    setMetadata(null);
    setJob(null);
    setJobId("");
    setViewState("analyzing");
    try {
      const info = await getInfo(url, token);
      setMetadata(info);
      setQuality(info.availableQualities?.[0] || "best");
      setViewState("ready");
    } catch (err) {
      handleApiError(err);
    }
  }

  async function handleStartDownload() {
    setError("");
    setNotice("");
    setJob(null);
    setViewState("downloading");
    try {
      const result = await startDownload(url, quality, token);
      setJobId(result.jobId);
      setJob({ jobId: result.jobId, status: result.status, progress: 0, message: "En cola..." });
    } catch (err) {
      handleApiError(err);
    }
  }

  async function handleSaveFile() {
    if (!jobId) {
      return;
    }

    setIsSaving(true);
    setError("");
    setNotice("");
    try {
      const { blob, filename } = await downloadFile(jobId, token);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1200);
      setNotice("Archivo entregado. Se borrara del servidor automaticamente.");
      setJob((current) =>
        current
          ? { ...current, status: "expired", message: "Archivo entregado.", progress: 100 }
          : current,
      );
    } catch (err) {
      handleApiError(err, null);
    } finally {
      setIsSaving(false);
    }
  }

  async function handleClear() {
    if (jobId && viewState === "downloading") {
      try {
        await cancelJob(jobId, token);
      } catch {
        // The cleanup timer will remove stale temporary files if the cancel request cannot finish.
      }
    }
    setUrl("");
    setQuality("best");
    setMetadata(null);
    setJob(null);
    setJobId("");
    setError("");
    setNotice("");
    setViewState(token ? "idle" : "loggedOut");
  }

  function handleLogout() {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setViewState("loggedOut");
    setMetadata(null);
    setJob(null);
    setJobId("");
    setNotice("");
    setError("");
  }

  function handleApiError(err, fallbackState = "error") {
    if (err?.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      setToken("");
      setViewState("loggedOut");
      setMetadata(null);
      setJob(null);
      setJobId("");
      setNotice("");
      setError("Sesion expirada. Volve a entrar.");
      return;
    }

    setError(err?.message || "No se pudo completar la accion.");
    if (fallbackState) {
      setViewState(fallbackState);
    }
  }

  if (!token || viewState === "loggedOut") {
    return (
      <main className="app-shell auth-shell">
        <section className="auth-panel" aria-labelledby="login-title">
          <div className="brand-mark" aria-hidden="true">
            MD
          </div>
          <h1 id="login-title">Media Downloader</h1>
          <p className="muted">App privada para guardar contenido publico permitido.</p>
          <form className="stack" onSubmit={handleLogin}>
            <input
              type="hidden"
              name="username"
              autoComplete="username"
              value="private-user"
              readOnly
            />
            <label htmlFor="password">Contrasena</label>
            <input
              id="password"
              name="password"
              autoComplete="current-password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Tu APP_PASSWORD"
            />
            <button className="primary-button" type="submit" disabled={!password.trim()}>
              Entrar
            </button>
          </form>
          {error ? <p className="error-text">{error}</p> : null}
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Privado y temporal</p>
          <h1>Media Downloader</h1>
        </div>
        <button className="ghost-button compact" type="button" onClick={handleLogout}>
          Salir
        </button>
      </header>

      <form className="download-form" onSubmit={handleAnalyze}>
        <label htmlFor="media-url">Link publico</label>
        <textarea
          id="media-url"
          rows="3"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="Pega un link de YouTube, TikTok, Instagram o X"
          disabled={isBusy}
        />
        <div className="action-row">
          <button className="primary-button" type="submit" disabled={!url.trim() || isBusy}>
            {viewState === "analyzing" ? "Analizando..." : "Analizar"}
          </button>
          <button className="ghost-button" type="button" onClick={handleClear} disabled={isSaving}>
            Limpiar
          </button>
        </div>
      </form>

      <p className="legal-note">Usa esto solo con contenido que tengas derecho a guardar.</p>

      {metadata ? (
        <section className="result-panel" aria-labelledby="result-title">
          <div className="media-summary">
            {metadata.thumbnail ? (
              <img src={metadata.thumbnail} alt="" className="thumbnail" loading="lazy" />
            ) : (
              <div className="thumbnail empty-thumb" aria-hidden="true">
                Video
              </div>
            )}
            <div>
              <p className="eyebrow">{metadata.site}</p>
              <h2 id="result-title">{metadata.title}</h2>
              <dl className="meta-list">
                {metadata.uploader ? (
                  <>
                    <dt>Autor</dt>
                    <dd>{metadata.uploader}</dd>
                  </>
                ) : null}
                {metadata.duration ? (
                  <>
                    <dt>Duracion</dt>
                    <dd>{formatDuration(metadata.duration)}</dd>
                  </>
                ) : null}
              </dl>
            </div>
          </div>

          <div className="quality-block">
            <label htmlFor="quality">Calidad</label>
            <select
              id="quality"
              value={quality}
              onChange={(event) => setQuality(event.target.value)}
              disabled={isBusy}
            >
              {qualities.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <button
            className="primary-button full-width"
            type="button"
            onClick={handleStartDownload}
            disabled={isBusy || viewState === "done"}
          >
            {viewState === "downloading" ? "Descargando..." : "Descargar"}
          </button>
        </section>
      ) : null}

      {job ? (
        <section className="progress-panel" aria-live="polite">
          <div className="progress-header">
            <span>{job.message || "Procesando..."}</span>
            <strong>{job.progress || 0}%</strong>
          </div>
          <div className="progress-track">
            <div className="progress-bar" style={{ width: `${job.progress || 0}%` }} />
          </div>
          {viewState === "done" ? (
            <button
              className="primary-button full-width"
              type="button"
              onClick={handleSaveFile}
              disabled={isSaving}
            >
              {isSaving ? "Preparando..." : "Guardar archivo"}
            </button>
          ) : null}
        </section>
      ) : null}

      {notice ? <p className="notice-text">{notice}</p> : null}
      {error ? <p className="error-text">{error}</p> : null}
    </main>
  );
}

function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) {
    return "";
  }
  const minutes = Math.floor(total / 60);
  const remaining = Math.floor(total % 60)
    .toString()
    .padStart(2, "0");
  return `${minutes}:${remaining}`;
}
