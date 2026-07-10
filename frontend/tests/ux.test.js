import test from "node:test";
import assert from "node:assert/strict";
import {
  PHASE_LABELS,
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
} from "../src/ux.js";

test("720p is recommended and selected even when the API lists best first", () => {
  const available = ["best", "1080", "720", "480", "mp3"];
  assert.equal(chooseDefaultQuality(available), "720");
  assert.equal(getAvailableQualityOptions(available)[0].value, "720");
  assert.match(getAvailableQualityOptions(available)[0].label, /Recomendado/);
});

test("quality selection falls back safely when 720p is unavailable", () => {
  assert.equal(chooseDefaultQuality(["480", "mp3"]), "480");
  assert.equal(getDownloadButtonLabel("480"), "Descargar en 480p");
  assert.equal(getDownloadButtonLabel("mp3"), "Descargar y convertir a MP3");
});

test("best quality and MP3 explain their performance cost", () => {
  assert.match(getQualityHint("best"), /archivo grande.*tardar/i);
  assert.match(getQualityHint("mp3"), /tarda más.*convierte/i);
});

test("all required progress phases have clear labels", () => {
  const required = [
    "validating_url",
    "normalizing_url",
    "extracting_metadata",
    "selecting_format",
    "downloading",
    "merging",
    "converting_audio",
    "preparing_file",
    "done",
    "error",
    "expired",
  ];
  for (const phase of required) {
    assert.ok(PHASE_LABELS[phase], `missing label for ${phase}`);
    assert.equal(getPhaseLabel({ phase }), PHASE_LABELS[phase]);
  }
});

test("progress values are clamped and missing yt-dlp percentages stay indeterminate", () => {
  assert.equal(getProgressValue({ progress: 147 }), 100);
  assert.equal(getProgressValue({ progress: -2 }), 0);
  assert.equal(getDownloadPercent({ downloadPercent: null }), null);
  assert.equal(getDownloadPercent({ downloadPercent: 42.25 }), 42.25);
});

test("download sizes and durations are mobile-friendly", () => {
  assert.equal(formatBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatDuration(65), "1:05");
  assert.equal(formatDuration(3665), "1:01:05");
});

test("YouTube rejected-cookie and verification errors remain distinct", () => {
  assert.equal(
    formatApiMessage("YouTube rechazó estas cookies durante la descarga."),
    "YouTube rechazó estas cookies. Exportá cookies nuevas y redeployá.",
  );
  assert.equal(
    formatApiMessage("YouTube pidió verificación de cuenta/no-bot."),
    "YouTube pidió verificación. Configurá cookies de YouTube en Render.",
  );
  assert.match(
    getYoutubeSupportNotice(
      "Las cookies de YouTube están activadas, pero el archivo no existe o no se puede leer.",
    ),
    /Secret File.*legible/i,
  );
});

test("single-use file expiration has an actionable message", () => {
  assert.equal(
    formatApiMessage("Archivo expirado. Genera una nueva descarga."),
    "Este archivo ya fue entregado o expiró. Generá una nueva descarga.",
  );
});
