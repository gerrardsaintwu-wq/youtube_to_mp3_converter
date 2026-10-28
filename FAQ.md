# YouToMedia FAQ

## What is YouToMedia?

YouToMedia is a web app that lets you convert YouTube videos and playlists to MP3 (audio) or MP4 (video). It uses a Flask API on the backend, [yt-dlp](https://github.com/yt-dlp/yt-dlp) to fetch media/metadata, and [ffmpeg](https://ffmpeg.org/) to extract/mux and embed metadata/thumbnails. The frontend is a simple HTML/JS UI served by Flask, with a PWA/service worker for mobile installs.

## Using the app

1. **Paste a link** - Paste a YouTube video URL or a public playlist URL into the input and click Analyze.
2. **Review results** - For a video you'll see a preview and size estimate. For playlists you'll see a searchable, chunked list (first 50 load, more load as you scroll). Selection state is tracked across the full list; the "Select Videos (x/y) - Total: ~..." counter reflects the full selected set and uses quality-aware size estimates.
3. **Choose options** - Pick format (MP3/MP4) and quality. For MP3: High (320 kbps), Medium (192 kbps), Low (128 kbps). For MP4: High (1080p), Medium (720p), Low (480p). Optionally embed metadata/thumbnails.
4. **Convert & download** - Click Convert. The job is queued and processed by workers; when complete you'll get a tokenized download link for a single file or a ZIP (for multiple selected videos).

### Limits (public users)

- **Playlists**: capped at **20 items** (non-admin). 
- **Duration**: videos longer than **30 minutes (1800s)** are skipped/rejected for public users.
- **Disk space**: the server enforces a **1 GB minimum free disk space** for non-admins (plus roughly **2.2× estimated size** of the job as a preflight). If not enough free space, conversion is rejected with a clear message.

### Admin / host access

If you're on the host machine (`127.0.0.1`/`localhost`), you can use the **"Toggle Public Limits"** button (bottom-right) to switch between **RESTRICTED** (same limits as above) and **UNLIMITED**. In unlimited mode there is no 20-item playlist cap and no 30-minute duration cap (very large playlists still take time to analyze/convert).

## How the conversion works (API journey)

All endpoints are served by the Flask app (`main.py`). The flow is: analyze → convert → download.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Renders the UI (`templates/index.html`). |
| `/api/analyze_start` | POST | Starts analysis for a URL. Body: `{"url": "..."}`. Returns `{"job_id": "..."}`. Admin detection: `request.remote_addr == '127.0.0.1'` or `global_unlimited_mode`. |
| `/api/analyze_status` | GET | Polls analysis progress. Query: `?job_id=...` Returns the job object (`status`, `progress`, `message`, `result`, or `error`). |
| `/api/convert` | POST | Queues a conversion. Body: `{"urls": [...], "format": "mp3"|"mp4", "quality": "high"|"medium"|"low", "metadata": bool, "playlist_title": str, "estimated_mb": number}`. Performs disk preflight (1 GB floor for non-admins + ~2.2× estimated size), creates a job with `status: queued`, submits to the download worker pool, returns `{"job_id": "..."}`. |
| `/api/status` | GET | Polls conversion progress. Query: `?job_id=...` Returns job status/progress/token/error. |
| `/download` | GET | Tokenized download. Query: `?token=...` Validates token via `access_manager` (must exist and not expired), serves the file/ZIP from `downloads/` as an attachment. |
| `/api/admin/toggle` | POST | Toggles `global_unlimited_mode`. Only allowed from `127.0.0.1`. |

### Worker pools

- **Analysis**: separate `ThreadPoolExecutor(max_workers=3)` — analysis jobs run here so long playlist analysis doesn't block downloads.
- **Downloads/Conversion**: `ThreadPoolExecutor(max_workers=4)` — conversion jobs are queued here (`status: queued` until a worker picks them up).

### Polling intervals (UI)

- Analysis status polled every **400ms**.
- Conversion status polled every **1500ms**.

## Inside the pipeline (conceptual)

### Analysis

1. **Fast playlist path (InnerTube)**: If the URL contains `list=` and parsing succeeds, the app tries `fetch_playlist_innertube()` which calls YouTube's InnerTube `browse` endpoint (no API key). It fetches the first page and remaining continuation pages in parallel (up to **4 workers**) to build the full list quickly. Results are filtered (private/deleted, duration limits for non-admins) and shaped to the same format the UI expects.
2. **yt-dlp fallback**: If InnerTube fails or it's not a standard playlist, it falls back to `yt_dlp.YoutubeDL` with `extract_flat: True`. For non-admins `playlistend` is set to **20** to avoid paging an entire large playlist just to reject it. Admins have no `playlistend` cap.
3. **Single video**: Extracts title/thumbnail/duration; non-admins rejected if duration > **1800s**.

### Conversion

1. **Job setup**: Creates `downloads/temp/<job_id>/`, configures yt-dlp options (format selection by quality, postprocessors, hooks, retries).
2. **yt-dlp options (key values)**: `quiet: True`, `ignoreerrors: True`, `nocheckcertificate: True`, `socket_timeout: 15`, `concurrent_fragment_downloads: 8`, `retries: 10` (network), `fragment_retries: 10`, `file_access_retries: 3` (Windows file lock/rename), `noprogress: True`.
3. **Format/quality**:
   - **MP3**: `bestaudio/best` + `FFmpegExtractAudio` (preferredcodec `mp3`, quality `320`/`192`/`128` for high/medium/low).
   - **MP4**: format selection prefers combined streams up to resolution (1080p/720p/480p), merges with `merge_output_format: mp4`.
4. **Metadata/thumbnails**: If enabled, adds `FFmpegMetadata` and `EmbedThumbnail` (and sets `writethumbnail: True`). If download yields nothing with embedding enabled, it falls back once without thumbnail embedding.
5. **Progress**: `progress_hooks` track download progress and update `job['progress']` (capped at 99 until done).
6. **Disk safety during job**: `progress_hook` checks free disk space every progress update and aborts if free space drops below **500 MB**.
7. **Output packaging**:
   - **Single URL**: moves the resulting file from temp to `downloads/` and registers it with `access_manager`.
   - **Multiple URLs / playlist selection**: creates a ZIP by streaming files into it and deleting each source file as it's added (to reduce peak disk usage). Registers the ZIP with `access_manager`.
8. **Cleanup**: Deletes the per-job temp directory on completion or error.

### Tokens & cleanup

- **Token creation**: `secrets.token_urlsafe(TOKEN_LENGTH)` where `TOKEN_LENGTH = 20` (from `constants.py`).
- **Expiration**: Tokens expire after **EXPIRY_TIME_MINUTES = 5** minutes (`constants.py`). `access_manager.manage_tokens()` runs in a background thread, removing expired tokens and deleting their associated files every **1 second**.
- **Download**: `/download` validates via `has_access()` and `is_valid()` before serving.

### Storage housekeeping

- **Preflight**: On `/api/convert`, disk usage is checked. For non-admins, required free space >= **1 GB** plus ~**2.2× estimated_mb**. Admins require a smaller floor (~200 MB). Requests are rejected with a clear message if insufficient.
- **Temp janitor**: A background thread (`cleanup_temp_dirs_loop`) runs hourly and deletes `downloads/temp/*` directories older than **3 hours** (to clean up crashed/interrupted jobs).
- **Temp structure**: `downloads/temp/<job_id>/` per job.

## How it's hosted

- **Local dev/production entry**: `serve.py` starts the server. It ensures `downloads/` exists, starts `access_manager.manage_tokens()` in a background thread, and runs the app with **Waitress**.
- **Binding**: Waitress listens on `host='0.0.0.0', port=5000` (`serve.py`).
- **Tunneling**: Commonly exposed externally via **ngrok** (pointed to port 5000).
- **PWA**: The app registers `static/sw.js` as a service worker (installed via `manifest.json`). On mobile, installing as a PWA can cache assets; if frontend behavior seems stale after updates, clearing site data/hard refresh may be needed.
- **Files of interest**: `main.py` (API/routes/workers), `serve.py` (Waitress), `access_manager.py` (tokens/expiry/cleanup), `constants.py` (paths/limits/codes), `templates/index.html` (UI logic), `static/sw.js` and `static/manifest.json` (PWA).
