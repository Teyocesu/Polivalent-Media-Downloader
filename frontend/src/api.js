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

export async function downloadFile(jobId, token) {
  const response = await fetch(`${API_BASE}/api/files/${encodeURIComponent(jobId)}`, {
    headers: authHeaders(token),
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }

  const disposition = response.headers.get("content-disposition") || "";
  const filename = parseFilename(disposition) || "media-download";
  const blob = await response.blob();
  return { blob, filename };
}

async function request(path, { method, body, token }) {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(token),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }

  return response.json();
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

function parseFilename(disposition) {
  const utfMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utfMatch) {
    return decodeURIComponent(utfMatch[1]);
  }
  const asciiMatch = disposition.match(/filename="?([^";]+)"?/i);
  return asciiMatch ? asciiMatch[1] : null;
}
