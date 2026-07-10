# Polivalent Media Downloader

Aplicación web privada para guardar videos desde el celular mediante un link. Usa FastAPI, React/Vite, yt-dlp, ffmpeg y un runtime Node compatible con el solver EJS de YouTube.

La aplicación está pensada para contenido público, contenido propio o contenido que una cuenta autorizada pueda ver legítimamente con sus propias cookies. No intenta evadir DRM, paywalls, acceso privado no autorizado ni restricciones legales.

## Compatibilidad y límites de seguridad

La allowlist admite estas plataformas:

- YouTube: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com` y `youtu.be`.
- TikTok: `tiktok.com`, `www.tiktok.com`, `vm.tiktok.com` y `vt.tiktok.com`.
- Instagram: `instagram.com` y `www.instagram.com`.
- X/Twitter: `x.com`, `www.x.com`, `twitter.com` y `www.twitter.com`.

Sólo se aceptan enlaces HTTPS. Los links de YouTube con un video individual se normalizan a `https://www.youtube.com/watch?v=VIDEO_ID`; se eliminan parámetros como `list`, `index`, `start_radio`, `si`, `pp` y `feature`. También se aceptan Shorts y `youtu.be`. Una playlist sin `v` se rechaza y yt-dlp siempre usa `noplaylist`. En las demás plataformas se eliminan query strings y fragmentos de tracking antes de llamar a yt-dlp.

No se habilitan sitios genéricos. Aunque yt-dlp los soporte, validar únicamente el dominio inicial no impide que un redirect o DNS rebinding alcance una IP privada. Mantener la allowlist cerrada evita convertir el servidor en un vector SSRF.

## Descargas temporales de único uso

- No hay base de datos, historial ni biblioteca.
- Cada trabajo recibe un ID aleatorio y una carpeta con permisos privados bajo `/tmp/media-downloads`.
- Se admiten hasta tres trabajos pendientes o listos a la vez; el worker procesa uno por vez para no saturar el servidor gratuito.
- El archivo se entrega como attachment por streaming directo, sin copiarlo completo a la RAM del teléfono, y no admite rangos parciales.
- La primera entrega consume el archivo; un segundo intento responde `410 Gone` mientras el job siga registrado.
- Cancelar o borrar un trabajo elimina sus parciales.
- Los fallos y timeouts limpian la carpeta temporal.
- El cleanup periódico elimina trabajos y carpetas huérfanas vencidas.
- No se debe agregar Persistent Disk en Render.

Si el cliente corta la conexión antes de completar la entrega, el cleanup periódico funciona como red de seguridad.

## Variables de entorno

Copiá `.env.example` a `.env` y reemplazá los valores de ejemplo.

| Variable | Default | Uso |
| --- | --- | --- |
| `APP_PASSWORD` | sólo local: contraseña de desarrollo | Obligatoria en producción. Usá una contraseña aleatoria y larga. |
| `APP_SECRET_KEY` | `APP_PASSWORD` | Clave para firmar sesiones. En producción conviene una clave aleatoria distinta de al menos 32 bytes. |
| `SESSION_TTL_SECONDS` | `86400` | Duración de la sesión. |
| `MAX_FILE_MB` | `500` | Tamaño máximo estimado y final. |
| `DOWNLOAD_TTL_MINUTES` | `15` | Antigüedad máxima de jobs y temporales. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `600` | Límite cooperativo por fase de metadata/descarga; se combina con timeouts de socket y reintentos acotados. |
| `YTDLP_SOCKET_TIMEOUT_SECONDS` | `20` | Timeout de red por operación de yt-dlp. |
| `DOWNLOAD_BASE_DIR` | `/tmp/media-downloads` | Carpeta efímera. No apuntar a un disco persistente en Render. |
| `ALLOWED_ORIGINS` | vacío | Orígenes CORS separados por coma. Para el frontend servido por la misma app puede quedar vacío. |
| `PORT` | `8000` | Puerto HTTP. Render lo define automáticamente. |
| `ENVIRONMENT` | `development` | Usar `production` en Render. |
| `YOUTUBE_COOKIES_ENABLED` | `false` | Activa cookies sólo para links de YouTube. |
| `YOUTUBE_COOKIES_PATH` | vacío | Ruta del archivo Netscape en runtime. |
| `YOUTUBE_COOKIES_FROM_BROWSER` | `none` | Sólo desarrollo local: `chrome`, `firefox`, `safari`, `edge` o `brave`. Se ignora en producción. |

Configuración recomendada para Render:

```text
APP_PASSWORD=<contraseña-aleatoria-larga>
APP_SECRET_KEY=<otra-clave-aleatoria-larga>
SESSION_TTL_SECONDS=86400
MAX_FILE_MB=500
DOWNLOAD_TTL_MINUTES=15
DOWNLOAD_TIMEOUT_SECONDS=600
YTDLP_SOCKET_TIMEOUT_SECONDS=20
ENVIRONMENT=production
YOUTUBE_COOKIES_ENABLED=true
YOUTUBE_COOKIES_PATH=/etc/secrets/youtube-cookies.txt
YOUTUBE_COOKIES_FROM_BROWSER=none
```

No agregues `DOWNLOAD_BASE_DIR` ni `PORT` en Render salvo que tengas un motivo concreto.

## Uso local con Docker

Requisitos: Docker Desktop o Docker Engine con Compose.

```bash
cp .env.example .env
docker compose up --build
```

Abrí [http://localhost:8000](http://localhost:8000). Desde un teléfono en la misma red, usá `http://IP-DE-TU-PC:8000`.

Para probar cookies en Docker sin copiarlas a la imagen:

```bash
docker build -t polivalent-media-downloader .
docker run --rm -p 8000:8000 --env-file .env \
  -e YOUTUBE_COOKIES_ENABLED=true \
  -e YOUTUBE_COOKIES_PATH=/run/secrets/youtube-cookies.txt \
  -v "$PWD/youtube-cookies.txt:/run/secrets/youtube-cookies.txt:ro" \
  polivalent-media-downloader
```

En PowerShell, reemplazá `$PWD/youtube-cookies.txt` por la ruta absoluta del archivo. No montes `/tmp/media-downloads` como `tmpfs`: un video grande consumiría RAM. La capa escribible descartable del contenedor ya es almacenamiento efímero.

## Uso local sin Docker

Necesitás Python 3.12+, Node 24+ y ffmpeg/ffprobe.

```bash
cp .env.example .env

cd frontend
npm ci
npm run check
npm run build

cd ..
rm -rf backend/static
cp -R frontend/dist backend/static

python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
APP_PASSWORD=tu-contraseña APP_SECRET_KEY=tu-clave \
  uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

En Windows PowerShell, activá el entorno con `.venv\Scripts\Activate.ps1` y definí variables con `$env:APP_PASSWORD = "..."`.

## Cookies de YouTube

Las cookies se leen sólo en runtime. No se copian durante el build, no se muestran en `/api/debug/system` y no deben entrar al repositorio.

Usá preferentemente una cuenta secundaria: YouTube puede invalidar cookies o limitar una cuenta usada por automatización.

Referencia: [guía oficial de yt-dlp para exportar cookies de YouTube](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

### Exportar un archivo pequeño y válido

1. Abrí una única ventana privada/incógnito.
2. Iniciá sesión con la cuenta secundaria.
3. En esa misma pestaña abrí `https://www.youtube.com/robots.txt`.
4. Exportá sólo las cookies del sitio `youtube.com` en formato Mozilla/Netscape.
5. Cerrá inmediatamente toda la ventana privada para que esa sesión no rote las cookies.
6. Confirmá que la primera línea sea `# Netscape HTTP Cookie File` o `# HTTP Cookie File`.
7. Verificá que el archivo sea menor a 500 KiB.

macOS/Linux:

```bash
wc -c youtube-cookies.txt
```

PowerShell:

```powershell
(Get-Item .\youtube-cookies.txt).Length
```

El deploy auditado fallaba antes de ejecutar el Dockerfile con `secret ... too big. max size 500KiB`. Un export de todas las cookies del navegador puede superar ese límite; exportá únicamente las de YouTube.

### Secret File exacto en Render

En `Environment > Secret Files`:

- Filename exacto: `youtube-cookies.txt`
- Ruta en runtime: `/etc/secrets/youtube-cookies.txt`
- Sin punto final, espacios, comillas ni prefijos de carpeta en el campo Filename.

Después guardá los cambios y hacé `Manual Deploy > Deploy latest commit`. El archivo no existe durante el build Docker; Render lo monta en runtime. Si falta o no es legible, la app inicia igual y reporta `youtubeCookiesReadable=false`.

Referencia: [Secret Files en Render](https://render.com/docs/configure-environment-variables#secret-files).

## Diagnóstico seguro

`GET /api/debug/system` siempre requiere un Bearer token válido. La respuesta no incluye cookies, claves, rutas absolutas ni tokens. Informa:

- versión de yt-dlp;
- disponibilidad de ffmpeg, ffprobe, Node y Deno, más versión y compatibilidad de Node 22+;
- si el almacenamiento temporal está listo y es efímero;
- límites de tamaño y timeout;
- ambiente;
- `youtubeCookiesEnabled`, `youtubeCookiesConfigured`, `youtubeCookiesReadable` y `youtubeCookiesMode`.

Ejemplo:

```bash
curl -sS https://TU-SERVICIO.onrender.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"TU_APP_PASSWORD"}'
```

Copiá el valor `token` de la respuesta y consultá:

```bash
curl -sS https://TU-SERVICIO.onrender.com/api/debug/system \
  -H 'Authorization: Bearer TU_TOKEN'
```

Resultados esperados con el Secret File correcto:

```json
{
  "ffmpegAvailable": true,
  "ffprobeAvailable": true,
  "nodeAvailable": true,
  "nodeVersion": "24.0.0",
  "nodeSupported": true,
  "tempStorageReady": true,
  "tempStorageEphemeral": true,
  "youtubeCookiesEnabled": true,
  "youtubeCookiesConfigured": true,
  "youtubeCookiesReadable": true,
  "youtubeCookiesMode": "file"
}
```

## Deploy en Render

1. Runtime: Docker.
2. Branch: `main`.
3. Root Directory: vacío.
4. Dockerfile Path: `./Dockerfile`.
5. Docker Build Context Directory: `.`.
6. Docker Command: vacío, para usar el `CMD` de la imagen.
7. Health Check Path: `/health`.
8. Auto-Deploy: `On Commit`.
9. Persistent Disk: ninguno.
10. Variables y Secret File: según las secciones anteriores.

La imagen construye el frontend, instala ffmpeg/ffprobe, usa Python 3.12 y copia Node 24 al runtime. `yt-dlp[default,curl-cffi]` instala la versión compatible de `yt-dlp-ejs`; no se instala `yt-dlp-ejs` por separado.

Referencia: [requisitos actuales del solver EJS de yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Actualizar yt-dlp

Las dependencias principales de producción están fijadas para evitar actualizaciones directas inesperadas; las transitivas se resuelven durante el build y deben volver a auditarse. Para actualizar yt-dlp:

1. Cambiá sólo el pin `yt-dlp[default,curl-cffi]==...` en `backend/requirements.txt`.
2. Ejecutá los tests y el build Docker.
3. Confirmá dentro del contenedor las versiones de yt-dlp, Node, ffmpeg y ffprobe.
4. Commiteá y redeployá.

No fijes `yt-dlp-ejs` manualmente: el extra `default` resuelve la versión compatible.

## QA local

```bash
python3 -m compileall -q backend
python3 -m pip install -r backend/requirements-dev.txt
pytest -q

cd frontend
npm ci
npm run check
npm run build

cd ..
git diff --check
docker build -t polivalent-media-downloader .
```

Con el contenedor levantado:

```bash
curl -f http://127.0.0.1:8000/health
```

## Errores esperables

- Cookies ausentes: la app sigue viva; YouTube explica cómo configurarlas.
- Cookies vencidas o rechazadas: exportá cookies nuevas y redeployá.
- Verificación no-bot/login: configurá cookies propias de YouTube.
- DRM, paywall o privado sin autorización: no se descarga.
- Restricción regional, de edad o de cuenta: puede seguir fallando aunque existan cookies.
- Instagram, TikTok y X cambian con frecuencia y pueden exigir login o bloquear IPs de datacenter.
- Algunos formatos requieren ffmpeg para unir audio y video; MP3 tarda más porque convierte audio.
- `best` puede tardar y pesar bastante más que 720p.
- Render Free puede dormirse y demorar el primer request.
- El timeout de aplicación se controla antes, después y durante el progreso de yt-dlp. Una operación nativa de ffmpeg o red que no emita callbacks puede demorar en responder a la cancelación hasta que finalice su timeout interno.

No existe garantía de que un extractor externo continúe funcionando después de cambios de la plataforma. Actualizá yt-dlp y conservá mensajes de error seguros, sin desactivar las barreras legales o de red.
