const API_BASE = "";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function login(password) {
  return request("/api/auth/login", {
    method: "POST",
    body: { password },
    token: null,
  });
}

export async function getInfo(url, token) {
  return request("/api/info", {
    method: "POST",
    body: { url },
    token,
  });
}

export async function startDownload(url, quality, token) {
  return request("/api/download", {
    method: "POST",
    body: { url, quality },
    token,
  });
}

export async function getJob(jobId, token) {
  return request(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: "GET",
    token,
  });
}

export async function cancelJob(jobId, token) {
  return request(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
    token,
  });
}

export async function authorizeFileDownload(jobId, token) {
  const result = await request(`/api/files/${encodeURIComponent(jobId)}/authorize`, {
    method: "POST",
    token,
  });
  if (!result?.downloadUrl) {
    throw new ApiError("El servidor no pudo preparar la entrega del archivo.", 500);
  }
  return resolveDownloadUrl(result.downloadUrl, window.location.origin);
}

async function request(path, { method, body, token }) {
  const response = await fetchFromApi(`${API_BASE}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(token),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }

  return response.json();
}

async function fetchFromApi(path, options) {
  try {
    return await fetch(path, options);
  } catch (error) {
    if (error instanceof TypeError) {
      throw new ApiError(
        "No se pudo conectar con el servidor. Revisá tu conexión e intentá de nuevo.",
        0,
      );
    }
    throw error;
  }
}

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function readError(response) {
  try {
    const data = await response.json();
    return data.detail || "No se pudo completar la accion.";
  } catch {
    return "No se pudo completar la accion.";
  }
}

export function resolveDownloadUrl(downloadUrl, origin) {
  const expectedOrigin = new URL(origin).origin;
  const resolved = new URL(downloadUrl, expectedOrigin);
  if (
    resolved.origin !== expectedOrigin ||
    !/^\/api\/files\/[A-Za-z0-9_-]+$/.test(resolved.pathname) ||
    resolved.search ||
    resolved.hash
  ) {
    throw new ApiError("El servidor devolvió una URL de entrega no válida.", 500);
  }
  return resolved.href;
}
