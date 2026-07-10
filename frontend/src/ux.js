export const DEFAULT_QUALITY = "720";

export const QUALITY_OPTIONS = [
  {
    value: "720",
    label: "720p · Recomendado",
    hint: "Recomendado para celular: buen equilibrio entre calidad, tamaño y velocidad.",
  },
  {
    value: "best",
    label: "Mejor calidad disponible",
    hint: "Mejor calidad puede generar un archivo grande y tardar bastante más.",
  },
  {
    value: "1080",
    label: "1080p",
    hint: "1080p usa más datos y puede tardar más que 720p.",
  },
  {
    value: "480",
    label: "480p · Archivo más liviano",
    hint: "480p prioriza una descarga más rápida y un archivo más liviano.",
  },
  {
    value: "mp3",
    label: "Solo audio MP3",
    hint: "MP3 tarda más porque primero descarga y después convierte el audio.",
  },
];

export const PHASE_LABELS = {
  queued: "En cola",
  validating_url: "Validando link",
  normalizing_url: "Normalizando URL",
  extracting_metadata: "Leyendo metadata",
  selecting_format: "Seleccionando formato",
  downloading: "Descargando video/audio",
  downloading_video: "Descargando video",
  downloading_audio: "Descargando audio",
  postprocessing: "Procesando archivo",
  merging: "Uniendo audio y video",
  converting_audio: "Convirtiendo audio a MP3",
  preparing_file: "Preparando archivo",
  done: "Listo para guardar",
  error: "Error",
  expired: "Expirado",
  delivered: "Entregado y eliminado",
};

export function getAvailableQualityOptions(availableQualities) {
  if (!Array.isArray(availableQualities) || availableQualities.length === 0) {
    return QUALITY_OPTIONS;
  }

  const available = new Set(availableQualities);
  const options = QUALITY_OPTIONS.filter((option) => available.has(option.value));
  return options.length ? options : QUALITY_OPTIONS;
}

export function chooseDefaultQuality(availableQualities) {
  const options = getAvailableQualityOptions(availableQualities);
  return options.some((option) => option.value === DEFAULT_QUALITY)
    ? DEFAULT_QUALITY
    : options[0]?.value || DEFAULT_QUALITY;
}

export function getQualityHint(quality) {
  return (
    QUALITY_OPTIONS.find((option) => option.value === quality)?.hint ||
    "Elegí la calidad que mejor se adapte a tu conexión."
  );
}

export function getDownloadButtonLabel(quality) {
  if (quality === "mp3") {
    return "Descargar y convertir a MP3";
  }
  if (quality === "best") {
    return "Descargar en mejor calidad";
  }
  return `Descargar en ${quality}p`;
}

export function getPhaseLabel(job) {
  if (!job) {
    return "Procesando";
  }
  return PHASE_LABELS[job.phase] || job.phaseLabel || job.message || "Procesando";
}

export function getProgressValue(job) {
  const value = Number(job?.progress);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : 0;
}

export function getDownloadPercent(job) {
  if (job?.downloadPercent == null) {
    return null;
  }
  const value = Number(job?.downloadPercent);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
}

export function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return "";
  }

  const units = ["B", "KB", "MB", "GB"];
  let current = size;
  let unit = 0;
  while (current >= 1024 && unit < units.length - 1) {
    current /= 1024;
    unit += 1;
  }
  return unit === 0
    ? `${Math.round(current)} ${units[unit]}`
    : `${current.toFixed(1)} ${units[unit]}`;
}

export function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) {
    return "";
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = Math.floor(total % 60)
    .toString()
    .padStart(2, "0");
  return hours
    ? `${hours}:${minutes.toString().padStart(2, "0")}:${remaining}`
    : `${minutes}:${remaining}`;
}

export function formatApiMessage(message) {
  const original = String(message || "No se pudo completar la acción.");
  if (isYoutubeCookieRejectedMessage(original)) {
    return "YouTube rechazó estas cookies. Exportá cookies nuevas y redeployá.";
  }
  if (isYoutubeVerificationMessage(original)) {
    return "YouTube pidió verificación. Configurá cookies de YouTube en Render.";
  }
  if (/archivo (?:ya no est[aá] disponible|expirado)|\b410\b/i.test(original)) {
    return "Este archivo ya fue entregado o expiró. Generá una nueva descarga.";
  }
  return original;
}

export function getYoutubeSupportNotice(message) {
  const original = String(message || "");
  if (isYoutubeCookieRejectedMessage(original)) {
    return "Las cookies de YouTube pueden vencer. Reemplazá el Secret File y redeployá Render.";
  }
  if (isYoutubeCookieUnavailableMessage(original)) {
    return "La app sigue disponible, pero YouTube puede requerir un Secret File de cookies legible.";
  }
  if (isYoutubeVerificationMessage(original)) {
    return "YouTube suele pedir esta verificación en servidores cloud como Render.";
  }
  return "";
}

export function isYoutubeCookieRejectedMessage(message) {
  return /youtube.*(?:rechaz|cookies?.*(?:venc|expir|caduc|inv[aá]lid))/i.test(message || "");
}

export function isYoutubeCookieUnavailableMessage(message) {
  return /cookies?.*(?:archivo.*(?:no existe|no se puede leer)|no (?:est[aá]n )?(?:disponibles|configuradas)|not (?:found|readable))/i.test(
    message || "",
  );
}

export function isYoutubeVerificationMessage(message) {
  return /youtube.*(?:verificaci[oó]n|no[- ]?bot|not a bot|confirm.*bot|sign in|login|inici[áa].*sesi[oó]n)/i.test(
    message || "",
  );
}
