import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const FRONTEND_ROOT = fileURLToPath(new URL("../", import.meta.url));

test("the app renders clear logged-out and authenticated mobile-first entry states", async (t) => {
  const server = await createServer({
    root: FRONTEND_ROOT,
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  t.after(async () => {
    await server.close();
    delete globalThis.localStorage;
  });

  const { default: App } = await server.ssrLoadModule("/src/App.jsx");

  globalThis.localStorage = { getItem: () => "", setItem() {}, removeItem() {} };
  const loggedOut = renderToStaticMarkup(React.createElement(App));
  assert.match(loggedOut, /Acceso privado/);
  assert.match(loggedOut, /Ingresar/);

  globalThis.localStorage = { getItem: () => "valid-token", setItem() {}, removeItem() {} };
  const authenticated = renderToStaticMarkup(React.createElement(App));
  assert.match(authenticated, /Cerrar sesión/);
  assert.match(authenticated, /Sin historial ni biblioteca/);
  assert.match(authenticated, /Cada archivo se guarda una sola vez/);
  assert.match(authenticated, /Analizar link/);
});
