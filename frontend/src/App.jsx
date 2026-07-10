import { useEffect, useMemo, useRef, useState } from "react";
import {
  authorizeFileDownload,
  cancelJob,
  getInfo,
  getJob,
  login,
  startDownload,
} from "./api.js";
import {
  DEFAULT_QUALITY,
  chooseDefaultQuality,
  formatApiMessage,
  formatBytes,
  formatDuration,
  getAvailableQualityOptions,
  getDownloadButtonLabel,
  getDownloadPercent,
  getPhaseLabel,
  getProgressValue,
  getQualityHint,
  getYoutubeSupportNotice,
} from "./ux.js";

const TOKEN_KEY = "private-media-downloader-token";
const TRANSFER_PHASES = new Set(["downloading", "downloading_video", "downloading_audio"]);

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [password, setPassword] = useState("");
  const [url, setUrl] = useState("");
  const [quality, setQuality] = useState(DEFAULT_QUALITY);
  const [viewState, setViewState] = useState(token ? "idle" : "loggedOut");
  const [metadata, setMetadata] = useState(null);
  const [job, setJob] = useState(null);
  const [jobId, setJobId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const operationIdRef = useRef(0);

  const isBusy = useMemo(
    () => ["analyzing", "downloading"].includes(viewState) || isSaving,
    [viewState, isSaving],
  );
  const availableQualities = useMemo(
    () => getAvailableQualityOptions(metadata?.availableQualities),
    [metadata?.availableQualities],
  );

  useEffect(() => {
    if (viewState !== "downloading" || !jobId || !token) {
      return undefined;
    }

    let cancelled = false;
    let polling = false;
    let consecutiveFailures = 0;
    const poll = async () => {
      if (polling) {
        return;
      }
      polling = true;
      try {
        const nextJob = await getJob(jobId, token);
        if (cancelled) {
          return;
        }
        consecutiveFailures = 0;
        setNotice((current) => (current.startsWith("Conexión inestable.") ? "" : current));
        setJob(nextJob);
        if (nextJob.status === "done") {
          setViewState("done");
        } else if (nextJob.status === "error" || nextJob.status === "expired") {
          const message = nextJob.message || "La descarga no está disponible.";
          setError(formatApiMessage(message));
          setNotice(getYoutubeSupportNotice(message));
          setViewState(nextJob.status === "expired" ? "expired" : "error");
        }
      } catch (err) {
        if (!cancelled) {
          const canRetry = !err?.status || err.status >= 500;
          consecutiveFailures += 1;
          if (canRetry && consecutiveFailures <= 3) {
            setNotice(
              `Conexión inestable. Reintentando estado de la descarga (${consecutiveFailures}/3)...`,
            );
          } else {
            handleApiError(err);
          }
        }
      } finally {
        polling = false;
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
      setError(formatApiMessage(err?.message));
    }
  }

  async function handleAnalyze(event) {
    event.preventDefault();
    const operationId = ++operationIdRef.current;
    const previousJobId = jobId;
    setError("");
    setNotice("");
    setMetadata(null);
    setJob(null);
    setJobId("");
    setViewState("analyzing");
    if (previousJobId) {
      void cancelJob(previousJobId, token).catch(() => undefined);
    }
    try {
      const info = await getInfo(url, token);
      if (operationIdRef.current !== operationId) {
        return;
      }
      setMetadata(info);
      if (info.normalizedUrl && info.normalizedUrl !== url.trim()) {
        setUrl(info.normalizedUrl);
        setNotice("URL de YouTube normalizada. Se usará únicamente el video indicado.");
      }
      setQuality(chooseDefaultQuality(info.availableQualities));
      setViewState("ready");
    } catch (err) {
      if (operationIdRef.current === operationId) {
        handleApiError(err);
      }
    }
  }

  async function handleStartDownload() {
    const operationId = ++operationIdRef.current;
    setError("");
    setNotice("");
    setJobId("");
    setJob({
      status: "downloading",
      phase: "validating_url",
      progress: 2,
      message: "Validando el link antes de descargar...",
      step: 1,
      stepsTotal: quality === "mp3" ? 6 : 5,
    });
    setViewState("downloading");
    try {
      const result = await startDownload(url, quality, token);
      if (operationIdRef.current !== operationId) {
        await cancelJob(result.jobId, token).catch(() => undefined);
        return;
      }
      setJobId(result.jobId);
      setJob((current) => ({
        ...current,
        jobId: result.jobId,
        status: result.status,
        phase: "queued",
        progress: 0,
        message: "En cola...",
      }));
    } catch (err) {
      if (operationIdRef.current === operationId) {
        handleApiError(err);
      }
    }
  }

  async function handleSaveFile() {
    if (!jobId || isSaving) {
      return;
    }

    setIsSaving(true);
    setError("");
    setNotice("");
    try {
      const downloadUrl = await authorizeFileDownload(jobId, token);
      const anchor = document.createElement("a");
      anchor.href = downloadUrl;
      anchor.download = "";
      anchor.rel = "noopener";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setViewState("delivered");
      setNotice(
        "La descarga del archivo comenzó. La copia temporal del servidor ya no puede volver a usarse.",
      );
      setJob((current) =>
        current
          ? {
              ...current,
              status: "expired",
              phase: "delivered",
              phaseLabel: "Entregado y eliminado",
              message: "Archivo entregado y eliminado del servidor.",
              progress: 100,
            }
          : current,
      );
    } catch (err) {
      setJob((current) =>
        current
          ? {
              ...current,
              status: err?.status === 410 ? "expired" : "error",
              phase: err?.status === 410 ? "expired" : "error",
              message: formatApiMessage(err?.message),
            }
          : current,
      );
      handleApiError(err, err?.status === 410 ? "expired" : "error");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleClear() {
    operationIdRef.current += 1;
    const activeJobId = jobId;
    const shouldCancel = activeJobId && !["delivered", "expired"].includes(viewState);
    resetDownloadState();
    if (shouldCancel) {
      await cancelJob(activeJobId, token).catch(() => undefined);
    }
  }

  function handleLogout() {
    operationIdRef.current += 1;
    const activeJobId = jobId;
    const activeToken = token;
    const shouldCancel = activeJobId && !["delivered", "expired"].includes(viewState);
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setPassword("");
    setViewState("loggedOut");
    setMetadata(null);
    setJob(null);
    setJobId("");
    setNotice("");
    setError("");
    if (shouldCancel) {
      void cancelJob(activeJobId, activeToken).catch(() => undefined);
    }
  }

  function resetDownloadState() {
    setUrl("");
    setQuality(DEFAULT_QUALITY);
    setMetadata(null);
    setJob(null);
    setJobId("");
    setError("");
    setNotice("");
    setViewState(token ? "idle" : "loggedOut");
  }

  function handleApiError(err, fallbackState = "error") {
    if (err?.status === 401) {
      operationIdRef.current += 1;
      localStorage.removeItem(TOKEN_KEY);
      setToken("");
      setViewState("loggedOut");
      setMetadata(null);
      setJob(null);
      setJobId("");
      setNotice("");
      setError("Tu sesión venció o no es válida. Volvé a ingresar.");
      return;
    }

    const message = err?.message || "No se pudo completar la acción.";
    setError(formatApiMessage(message));
    setNotice(getYoutubeSupportNotice(message));
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
          <p className="eyebrow">Acceso privado</p>
          <h1 id="login-title">Media Downloader</h1>
          <p className="muted" id="login-help">
            Ingresá con la contraseña configurada por el administrador.
          </p>
          <form className="stack" onSubmit={handleLogin}>
            <input
              type="hidden"
              name="username"
              autoComplete="username"
              value="private-user"
              readOnly
            />
            <label htmlFor="password">Contraseña</label>
            <input
              id="password"
              name="password"
              autoComplete="current-password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Tu APP_PASSWORD"
              aria-describedby="login-help"
            />
            <button className="primary-button" type="submit" disabled={!password.trim()}>
              Ingresar
            </button>
          </form>
          {error ? (
            <p className="error-text" role="alert">
              {error}
            </p>
          ) : null}
        </section>
      </main>
    );
  }

  const analysisJob =
    viewState === "analyzing"
      ? {
          status: "analyzing",
          phase: "extracting_metadata",
          progress: null,
          message: "Validando el link, normalizando la URL y leyendo metadata...",
        }
      : null;
  const visibleJob = job || analysisJob;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="title-block">
          <p className="eyebrow">Privado y temporal</p>
          <h1>Media Downloader</h1>
        </div>
        <button
          className="ghost-button compact logout-button"
          type="button"
          onClick={handleLogout}
          disabled={isSaving}
        >
          Cerrar sesión
        </button>
      </header>

      <section className="intro-panel" aria-label="Cómo funciona">
        <strong>Sin historial ni biblioteca.</strong>
        <span>Cada archivo se guarda una sola vez y después se elimina del servidor.</span>
      </section>

      <form className="download-form" onSubmit={handleAnalyze}>
        <label htmlFor="media-url">Link del video</label>
        <textarea
          id="media-url"
          rows="3"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="Pegá un link de YouTube, TikTok, Instagram o X"
          disabled={isBusy}
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
        />
        <div className="action-row">
          <button className="primary-button" type="submit" disabled={!url.trim() || isBusy}>
            {viewState === "analyzing" ? "Analizando link..." : "Analizar link"}
          </button>
          <button className="ghost-button" type="button" onClick={handleClear} disabled={isSaving}>
            {viewState === "downloading" ? "Cancelar y limpiar" : "Limpiar"}
          </button>
        </div>
      </form>

      <p className="legal-note">Usalo sólo con contenido que tengas derecho a guardar.</p>

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
            <div className="media-copy">
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
                    <dt>Duración</dt>
                    <dd>{formatDuration(metadata.duration)}</dd>
                  </>
                ) : null}
                {metadata.filesize ? (
                  <>
                    <dt>Tamaño estimado</dt>
                    <dd>{formatBytes(metadata.filesize)}</dd>
                  </>
                ) : null}
              </dl>
            </div>
          </div>

          <div className="quality-block">
            <div className="label-row">
              <label htmlFor="quality">Calidad</label>
              {quality === DEFAULT_QUALITY ? <span className="recommended-chip">Recomendada</span> : null}
            </div>
            <select
              id="quality"
              value={quality}
              onChange={(event) => setQuality(event.target.value)}
              disabled={isBusy}
            >
              {availableQualities.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <p
              className={`quality-hint ${["best", "mp3"].includes(quality) ? "is-warning" : ""}`}
            >
              {getQualityHint(quality)}
            </p>
          </div>

          <button
            className="primary-button full-width"
            type="button"
            onClick={handleStartDownload}
            disabled={isBusy || ["done", "delivered"].includes(viewState)}
          >
            {viewState === "downloading"
              ? "Descargando..."
              : getDownloadButtonLabel(quality)}
          </button>
        </section>
      ) : null}

      {visibleJob ? (
        <ProgressPanel
          job={visibleJob}
          viewState={viewState}
          isSaving={isSaving}
          onSave={handleSaveFile}
        />
      ) : null}

      {notice ? (
        <p className="notice-text" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
    </main>
  );
}

function ProgressPanel({ job, viewState, isSaving, onSave }) {
  const phaseLabel = getPhaseLabel(job);
  const progress = getProgressValue(job);
  const downloadPercent = getDownloadPercent(job);
  const isTransfer = TRANSFER_PHASES.has(job.phase);
  const isIndeterminate =
    viewState === "analyzing" ||
    (viewState === "downloading" && !(isTransfer && downloadPercent != null));
  const hasData = job.downloadedBytes != null || job.totalBytes != null;
  const filename = job.currentFile || job.filename;

  return (
    <section
      className="progress-panel"
      aria-labelledby="progress-title"
      aria-busy={["analyzing", "downloading"].includes(viewState)}
    >
      <div className="progress-header" aria-live="polite" aria-atomic="true">
        <div>
          <p className="progress-kicker">Fase actual</p>
          <h2 id="progress-title">{phaseLabel}</h2>
        </div>
        {isIndeterminate ? (
          <strong className="progress-percent">En curso</strong>
        ) : job.progress != null ? (
          <strong className="progress-percent">{progress}%</strong>
        ) : null}
      </div>

      <div
        className="progress-track"
        role="progressbar"
        aria-label={`Progreso: ${phaseLabel}`}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={isIndeterminate ? undefined : progress}
        aria-valuetext={isIndeterminate ? `${phaseLabel}, en curso` : `${progress}%`}
      >
        <div
          className={`progress-bar ${isIndeterminate ? "is-indeterminate" : ""}`}
          style={isIndeterminate ? undefined : { width: `${progress}%` }}
        />
      </div>

      <div className="progress-copy">
        {job.step && job.stepsTotal ? (
          <span className="step-chip">
            Paso {Math.min(job.step, job.stepsTotal)} de {job.stepsTotal}
          </span>
        ) : null}
        <p className="progress-message">{job.message || `${phaseLabel}...`}</p>
      </div>

      {isTransfer || hasData || job.speed || job.eta || filename ? (
        <dl className="download-stats">
          {isTransfer ? (
            <>
              <dt>Descarga</dt>
              <dd>
                {downloadPercent == null ? "Calculando porcentaje..." : `${downloadPercent.toFixed(1)}%`}
              </dd>
              <dt>Velocidad</dt>
              <dd>{job.speed || "Calculando..."}</dd>
              <dt>Tiempo restante</dt>
              <dd>{job.eta || "Calculando..."}</dd>
            </>
          ) : null}
          {isTransfer || hasData ? (
            <>
              <dt>Datos</dt>
              <dd>
                {hasData ? formatBytes(job.downloadedBytes || 0) : "Calculando tamaño..."}
                {hasData && job.totalBytes ? ` / ${formatBytes(job.totalBytes)}` : ""}
                {hasData && !job.totalBytes ? " descargados" : ""}
              </dd>
            </>
          ) : null}
          {filename ? (
            <>
              <dt>Archivo actual</dt>
              <dd>{filename}</dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {viewState === "done" ? (
        <div className="ready-card">
          <strong>Tu archivo está listo para guardar.</strong>
          <p>
            El botón funciona una sola vez. Al entregar el archivo, el servidor elimina su copia
            temporal.
          </p>
          <button
            className="primary-button full-width"
            type="button"
            onClick={onSave}
            disabled={isSaving}
          >
            {isSaving ? "Preparando entrega..." : "Guardar archivo (una sola vez)"}
          </button>
        </div>
      ) : null}

      {viewState === "delivered" ? (
        <div className="delivered-card" role="status">
          <strong>Archivo entregado.</strong>
          <p>La copia temporal fue eliminada. Para guardarlo otra vez, generá una nueva descarga.</p>
        </div>
      ) : null}
    </section>
  );
}
