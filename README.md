# Private Media Downloader

Mini web privada para descargar contenido publico permitido desde YouTube, TikTok, Instagram y X/Twitter. Usa FastAPI, React/Vite, yt-dlp y ffmpeg. No usa base de datos ni disco persistente.

Usala solo con contenido propio, publico o que tengas permiso de guardar. No intenta saltar DRM, paywalls, contenido privado, login obligatorio ni restricciones de acceso.

## Que hace

- Login con `APP_PASSWORD`.
- Analisis de metadata sin descargar archivos.
- Limpieza automatica de URLs de YouTube con parametros de playlist/radio/share cuando contienen un video individual.
- Selector de calidad: mejor compatible, 1080p, 720p, 480p o MP3.
- Progreso detallado con fase, velocidad, ETA y bytes cuando `yt-dlp` los reporta.
- Descarga en una carpeta temporal unica bajo `/tmp/media-downloads`.
- Entrega el archivo como attachment.
- Borra el archivo y la carpeta temporal despues de entregarlo.
- Expira temporales viejos cada 5 minutos.
- Mantiene solo estado temporal en memoria, sin historial permanente.

## Variables de entorno

Copiá `.env.example` a `.env` y ajustá:

```bash
APP_PASSWORD=cambia-esta-contrasena
APP_SECRET_KEY=cambia-esta-clave-para-firmar-tokens
MAX_FILE_MB=500
DOWNLOAD_TTL_MINUTES=15
DOWNLOAD_TIMEOUT_SECONDS=600
ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
PORT=8000
ENVIRONMENT=development
YOUTUBE_COOKIES_ENABLED=false
YOUTUBE_COOKIES_PATH=
YOUTUBE_COOKIES_FROM_BROWSER=none
```

En produccion, `APP_PASSWORD` es obligatoria. `APP_SECRET_KEY` es opcional, pero recomendado para firmar tokens con una clave distinta de la contrasena. En Render tambien conviene usar `ENVIRONMENT=production`.

## Correr local con Docker

```bash
cp .env.example .env
docker compose up --build
```

Abrí [http://localhost:8000](http://localhost:8000).

Desde un celular en la misma red Wi-Fi, buscá la IP local de tu computadora y abrí:

```text
http://IP-DE-LA-PC:8000
```

En macOS podés probar:

```bash
ipconfig getifaddr en0
```

## Correr local sin Docker

Necesitás Python 3.12+, Node 20+ y ffmpeg instalado. Node tambien ayuda a `yt-dlp-ejs` para compatibilidad con YouTube.

```bash
cp .env.example .env

cd frontend
npm install
npm run build

cd ..
rm -rf backend/static
cp -R frontend/dist backend/static

cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
APP_PASSWORD=tu-contrasena PORT=8000 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abrí [http://localhost:8000](http://localhost:8000).

Para desarrollo frontend con Vite:

```bash
cd backend
APP_PASSWORD=tu-contrasena uvicorn app.main:app --host 0.0.0.0 --port 8000

cd ../frontend
npm run dev
```

Vite proxya `/api` y `/health` al backend local.

## Deploy en Render

1. Subí este proyecto a GitHub.
2. En Render, creá un nuevo Web Service.
3. Elegí el repo y seleccioná Docker como runtime.
4. No agregues Persistent Disk.
5. Configurá variables:

```text
APP_PASSWORD=una-contrasena-larga
APP_SECRET_KEY=otra-clave-larga
MAX_FILE_MB=500
DOWNLOAD_TTL_MINUTES=15
DOWNLOAD_TIMEOUT_SECONDS=600
ENVIRONMENT=production
YOUTUBE_COOKIES_ENABLED=false
```

Render define `PORT` automaticamente. El backend escucha en `0.0.0.0` y usa esa variable.

La imagen Docker instala `ffmpeg`, `ffprobe`, `nodejs`, `yt-dlp[default,curl-cffi]` y `yt-dlp-ejs` en runtime. No usa cookies ni login por defecto.

## YouTube en Render: configurar cookies

Si YouTube responde `Sign in to confirm you're not a bot` desde Render, podés configurar cookies propias de YouTube como Secret File. Usá esto solo para contenido al que tu cuenta pueda acceder normalmente. No sirve para DRM, paywalls ni contenido inaccesible para esa cuenta.

Recomendacion fuerte: usá una cuenta secundaria de Google/YouTube, no tu cuenta principal.

1. Exportá un `cookies.txt` de YouTube en formato Netscape desde un navegador donde esa cuenta secundaria esté logueada.
2. No commitees ese archivo.
3. No lo subas al repo.
4. En Render, entrá al servicio.
5. Abrí `Environment`.
6. En `Secret Files`, elegí `Add Secret File`.
7. Usá `youtube-cookies.txt` como filename.
8. Pegá el contenido completo del `cookies.txt`.
9. Agregá estas variables:

```text
YOUTUBE_COOKIES_ENABLED=true
YOUTUBE_COOKIES_PATH=/etc/secrets/youtube-cookies.txt
YOUTUBE_COOKIES_FROM_BROWSER=none
```

10. Redeployá el servicio.

Las cookies pueden expirar o quedar invalidadas. Si YouTube vuelve a rechazar la descarga, exportá cookies nuevas, actualizá el Secret File y redeployá. Si YouTube cambia sus protecciones, puede seguir fallando aun con cookies.

Para desarrollo local sin Docker podés usar cookies del navegador local:

```bash
YOUTUBE_COOKIES_ENABLED=true
YOUTUBE_COOKIES_FROM_BROWSER=chrome
```

En Docker local es mejor montar un `youtube-cookies.txt` como volumen y apuntar `YOUTUBE_COOKIES_PATH` a esa ruta dentro del contenedor.

## Actualizar yt-dlp

Con Docker, reconstruí la imagen:

```bash
docker compose build --no-cache
docker compose up
```

Sin Docker:

```bash
cd backend
source .venv/bin/activate
pip install -U yt-dlp
```

En Render, hacé un nuevo deploy para que la imagen instale la version disponible de `yt-dlp`.

Si YouTube cambia algo y empieza a fallar, forzá un redeploy o un deploy manual desde Render para reconstruir la imagen e instalar una version nueva de `yt-dlp`. Las cookies de YouTube son opcionales y deben configurarse como Secret File, nunca en el repo.

## Debug seguro

`GET /api/debug/system` devuelve informacion de runtime sin secretos:

- version de `yt-dlp`
- disponibilidad de `ffmpeg` y `ffprobe`
- disponibilidad de Node/Deno para `yt-dlp-ejs`
- carpeta temporal
- limites de tamano y timeout
- entorno
- estado de cookies de YouTube: `youtubeCookiesEnabled`, `youtubeCookiesConfigured`, `youtubeCookiesReadable`, `youtubeCookiesMode`

En produccion requiere `Authorization: Bearer <token>`.

## QA local

Backend:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cd ..
pytest -q
```

Frontend:

```bash
cd frontend
npm install
npm run build
```

Docker, si Docker Desktop esta corriendo:

```bash
docker compose build
docker compose up
```

## Descargas temporales de unico uso

Cada descarga crea un `jobId` aleatorio y una carpeta propia en `/tmp/media-downloads`. Cuando el usuario guarda el archivo, el backend responde con el attachment y luego elimina la carpeta con una tarea de fondo. Si se intenta usar otra vez el mismo `jobId`, la API responde `410 Gone`.

Si la descarga falla, se limpia la carpeta parcial. Si se cierra la pagina o se corta la conexion, el cleanup automatico elimina temporales con mas de `DOWNLOAD_TTL_MINUTES`.

## Limitaciones

- Algunas plataformas pueden cambiar y romper temporalmente `yt-dlp`.
- Instagram o X/Twitter pueden requerir acceso especial para ciertos contenidos.
- Render Free puede dormirse y tardar en despertar.
- Videos grandes dependen del tamano, la red y el servidor.
- MP3 puede tardar mas porque requiere conversion.
- No hay historial: si se cierra la pagina, hay que generar la descarga otra vez.
