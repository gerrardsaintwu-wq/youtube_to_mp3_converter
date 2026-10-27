# Implementation Ledger — youtube_to_mp3_converter

## 1. System & Tech Stack
- **OS/Runtime:** Windows (win32), Python 3.14.6
- **Frameworks/Libraries:** Flask 3.1.3, waitress 3.0.2 (production WSGI server), yt-dlp 2026.10.21, FFmpeg 9.0.2 (Gyan essentials build, on PATH)
- **Environment constraints:** Windows batch (`.bat`) for server launch; host-only admin toggle guarded to `127.0.0.1`; token TTL = 5 minutes (`constants.py`)

## 2. The Blueprint
```
youtube_to_mp3_converter/
├── start_server.bat        # NEW — production launcher (checks python/ffmpeg/waitress, runs serve.py)
├── media_converter.bat     # legacy dev launcher (python main.py)
├── serve.py                # Waitress server on 0.0.0.0:5000, starts token manager thread
├── main.py                 # MODIFIED — Flask API; duplicate /api/convert route removed; yt-dlp speed opts added
├── constants.py            # paths, HTTP codes, token settings (ABS_DOWNLOADS_PATH, EXPIRY_TIME_MINUTES, TOKEN_LENGTH)
├── access_manager.py       # in-memory token ledger + 1s background cleanup loop
├── requirements.txt        # MODIFIED — added waitress==3.0.2
├── ledger.md               # agent handoff prompt template (not runtime code)
├── downloads/              # runtime output dir; jobs use downloads/temp/<job_id>/
├── static/ templates/      # frontend assets
└── *.bat / README.md / requirements.txt
```

## 3. Strict Code Contracts
- `POST /api/convert` → `{"urls": [str], "format": "mp3"|"mp4", "quality": "high"|"medium"|"low", "metadata": bool, "playlist_title": str}` → `200 {"job_id": uuid}`
- `GET /api/status?job_id=<id>` → `{"status": "queued"|"processing"|"done"|"error", "progress": int, "token": str?, "error": str?}`
- `GET /download?token=<token>` → file/ZIP if token valid & unexpired (401 otherwise), served from `ABS_DOWNLOADS_PATH`
- `POST /api/admin/toggle` (host-only, remote_addr == '127.0.0.1') → toggles `global_unlimited_mode`
- `process_media_job(job_id, urls, format_type, quality, embed_meta)`; jobs queued on `ThreadPoolExecutor(max_workers=3)`
- yt-dlp opts dict keys added: `concurrent_fragment_downloads=8`, `retries=5`, `fragment_retries=5`, `no_warnings=True`, `noprogress=True`

## 4. Sequential Implementation Steps
1. [x] Create `start_server.bat`: `cd /d %~dp0`, verify python + ffmpeg on PATH, auto-`pip install -r requirements.txt` on first run, launch `python serve.py`
2. [x] Remove the duplicate `@app.route("/api/convert")` definition in `main.py` (the second definition silently overrode the first, leaving the 3-worker executor dead code and spawning an unbounded raw thread per job)
3. [x] Add yt-dlp speed options to `process_media_job` opts (parallel fragment downloads, retries, quiet flags)
4. [x] Add `waitress==3.0.2` to `requirements.txt` (was implicit; `serve.py` imports it but it was unpinned/unlisted)
5. [x] Verify `main.py` compiles (`python -m py_compile main.py`)

## 5. Edge Cases & Logic Traps
- **Do not run in Flask debug mode for production:** `app.run(debug=True)` enables the reloader, which imports `main` twice and starts two token-manager threads. Always launch via `start_server.bat` → `serve.py`.
- **Empty-download fallback:** if `embed_meta` is on and yt-dlp's thumbnail embedder crashes, `downloads/temp/<job_id>/` may be empty; code retried the download without the `EmbedThumbnail` postprocessor — preserve this.
- **Token/token-file coupling:** expired tokens delete their referenced file after 5 min; a download in flight may fail if the user is slow — by design.
- **Never rename** `audio_files[token]` keys after `add_token`; `/download` uses `send_from_directory(ABS_DOWNLOADS_PATH, filename)`.
- Keep `safe_title` sanitization (`alnum` + ` _-`) before ZIP filename generation to avoid invalid Windows paths.
