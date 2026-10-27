"""
main.py - Advanced YouTube Audio/Video Converter API
Includes fixes for missing thumbnail freezes and socket timeouts.
"""

import secrets
import threading
import shutil
import re
import json
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from uuid import uuid4
import yt_dlp
import access_manager
from constants import *
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error
import os
import zipfile

# Global state to track if public users have unlimited access
global_unlimited_mode = False
app = Flask(__name__, static_folder='static', template_folder='templates')

jobs = {}
analysis_jobs = {}
# Create a strict limit of 4 concurrent workers (reduced to save disk space)
# Anything beyond this waits in the queue.
executor = ThreadPoolExecutor(max_workers=4)
# Separate executor for analysis so downloads aren't blocked by long playlist fetches
analysis_executor = ThreadPoolExecutor(max_workers=3)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/analyze_start", methods=["POST"])
def start_analysis():
    data = request.json
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify(error="No URL provided"), BAD_REQUEST

    # Host-machine users bypass the 30-min / 20-item limits
    is_admin = request.remote_addr == '127.0.0.1' or global_unlimited_mode

    job_id = str(uuid4())
    analysis_jobs[job_id] = {'status': 'starting', 'progress': 0, 'message': 'Starting...'}
    analysis_executor.submit(analyze_worker, job_id, url, is_admin)
    return jsonify(job_id=job_id)


@app.route("/api/convert", methods=["POST"])
def start_conversion():
    data = request.json
    urls = data.get("urls", [])
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "high")
    embed_meta = data.get("metadata", False)
    playlist_title = data.get("playlist_title", "YouTube_Media")
    estimated_mb = data.get("estimated_mb", 0)

    if not urls:
        return jsonify(error="No valid URLs provided"), BAD_REQUEST

    # Disk space preflight (1GB for non-admins)
    try:
        is_admin = request.remote_addr == '127.0.0.1' or global_unlimited_mode
        usage = shutil.disk_usage(ABS_DOWNLOADS_PATH)
        free_bytes = usage.free
        # Estimate needed: estimated * 2.2 for temp+zip overhead + buffer
        est_bytes = float(estimated_mb or 0) * 1024 * 1024 * 2.2
        min_free_bytes = 1024 * 1024 * 1024 if not is_admin else 200 * 1024 * 1024
        if est_bytes < 200 * 1024 * 1024:
            est_bytes = 200 * 1024 * 1024
        if free_bytes < est_bytes + min_free_bytes:
            free_gb = free_bytes / (1024**3)
            need_gb = (est_bytes + min_free_bytes) / (1024**3)
            return jsonify(error=f"Not enough disk space: need ~{need_gb:.2f} GB, only {free_gb:.2f} GB free."), 400
    except Exception:
        pass

    job_id = str(uuid4())
    safe_title = "".join(c for c in playlist_title if c.isalnum() or c in " _-").strip() or "Playlist"
    
    # Set initial status to 'queued' instead of 'processing'
    jobs[job_id] = {'status': 'queued', 'progress': 0, 'safe_title': safe_title}
    
    # Submit the job to the worker queue instead of starting it immediately
    executor.submit(process_media_job, job_id, urls, format_type, quality, embed_meta)
    
    return jsonify(job_id=job_id)

@app.route("/api/admin/toggle", methods=["POST"])
def toggle_admin():
    global global_unlimited_mode
    # Security: Ensure only the physical host machine can hit this endpoint
    if request.remote_addr != '127.0.0.1':
        return jsonify(error="Unauthorized. Only the host can toggle this."), 401
    
    global_unlimited_mode = not global_unlimited_mode
    state = "UNLIMITED" if global_unlimited_mode else "RESTRICTED (30-min / 20-item limit)"
    return jsonify(success=True, state=state, message=f"Public access is now {state}.")

@app.route("/api/analyze_status", methods=["GET"])
def analyze_status():
    job_id = request.args.get("job_id")
    job = analysis_jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), NOT_FOUND
    return jsonify(job)

def analyze_worker(job_id, url, is_admin):
    job = analysis_jobs[job_id]
    try:
        job['message'] = "Fetching media information..."
        playlist_id = extract_playlist_id(url)
        # Try InnerTube parallel pagination for playlists first (faster for different lists)
        if playlist_id:
            try:
                videos = fetch_playlist_innertube(playlist_id, is_admin)
                if videos:
                    # Apply filters same as before
                    filtered = []
                    for v in videos:
                        title = v.get('title')
                        if not title or title in ['[Private video]', '[Deleted video]']:
                            continue
                        duration = v.get('duration') or 0
                        if not is_admin and duration > 1800:
                            continue
                        filtered.append(v)
                    if not filtered:
                        raise Exception("The playlist contains no valid videos under the 30-minute duration limit.")
                    # Get playlist title best effort via yt-dlp flat single pass? or use generic
                    playlist_title = None
                    try:
                        ydl_opts_t = {'extract_flat': True, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': 10, 'playlistend': 1}
                        with yt_dlp.YoutubeDL(ydl_opts_t) as ydl_t:
                            info_t = ydl_t.extract_info(url, download=False)
                            if info_t and info_t.get('title'):
                                playlist_title = info_t.get('title')
                    except Exception:
                        pass
                    job['result'] = {
                        'type': 'playlist',
                        'title': playlist_title or 'YouTube Playlist',
                        'thumbnail': filtered[0]['thumbnail'] if filtered else '',
                        'videos': filtered
                    }
                    job['status'] = 'done'
                    job['message'] = "Analysis complete!"
                    return
            except Exception:
                # Fall through to yt-dlp path
                pass

        ydl_opts = {'extract_flat': True, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': 15}
        
        # For non-admins, stop yt-dlp from paging through the entire playlist
        # when we'd reject it anyway (20-item public limit). Admins get no cap:
        # they fetch however much the playlist contains.
        if not is_admin:
            ydl_opts['playlistend'] = 20
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("Could not retrieve information. The link may be dead or private.")

            if 'entries' in info:
                entries = list(info['entries'])
                
                # --- PLAYLIST LIMIT CHECK ---
                if not is_admin and len(entries) > 20:
                    raise Exception("Public playlists are limited to 20 items. Host access has unlimited downloads.")
                
                total_items = len(entries)
                
                if total_items == 0:
                    raise Exception("Playlist is empty or unavailable.")
                     
                videos = []
                
                for idx, e in enumerate(entries):
                    i = idx + 1
                    title = e.get('title')
                    
                    if e and e.get('url') and title and title not in ['[Private video]', '[Deleted video]']:
                        
                        # --- DURATION LIMIT CHECK (per video in playlist) ---
                        duration = e.get('duration', 0)
                        if not is_admin and duration > 1800: # 1800 seconds = 30 minutes
                            # Skip this video if it's too long
                            continue

                        thumb = e.get('thumbnail')
                        if not thumb and e.get('id'):
                            thumb = f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"
                            
                        videos.append({
                            'title': title,
                            'url': e.get('url'),
                            'thumbnail': thumb,
                            'duration': duration
                        })
                     
                    # Single lightweight progress update per item
                    job['message'] = f"Processing metadata ({i}/{total_items})"
                    job['progress'] = int(((i + 1) / total_items) * 100)
                     
                if not videos:
                    raise Exception("The playlist contains no valid videos under the 30-minute duration limit.")
                     
                job['result'] = {
                    'type': 'playlist',
                    'title': info.get('title', 'YouTube Playlist'),
                    'thumbnail': videos[0]['thumbnail'] if videos else '',
                    'videos': videos
                }
            else:
                title = info.get('title')
                if not title or title in ['[Private video]', '[Deleted video]']:
                    raise Exception("This video is private, deleted, or unavailable.")
                
                # --- NEW: DURATION LIMIT CHECK (single video) ---
                duration = info.get('duration', 0)
                if not is_admin and duration > 1800:
                    raise Exception("Public downloads are limited to 30 minutes. Host access has unlimited downloads.")
                
                job['message'] = "Fetching video (1/1)"
                job['progress'] = 33
                job['message'] = "Processing metadata (1/1)"
                job['progress'] = 66
                job['message'] = "Calculating size (1/1)"
                job['progress'] = 100
                
                job['result'] = {
                    'type': 'video',
                    'title': title,
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': duration,
                    'url': info.get('webpage_url') or url
                }
                
        job['status'] = 'done'
        job['message'] = "Analysis complete!"
        
    except Exception as e:
        error_msg = str(e)
        if "Sign in to confirm" in error_msg or "Private video" in error_msg:
            error_msg = "This content is private or restricted."
        elif "Video unavailable" in error_msg or "Incomplete YouTube ID" in error_msg:
            error_msg = "The link is dead, invalid, or unavailable."
        job['status'] = 'error'
        job['error'] = error_msg


def extract_playlist_id(url):
    try:
        m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def fetch_playlist_innertube(playlist_id, is_admin):
    """Fetch playlist via InnerTube browse API (no key). Falls back to raising on failure."""
    videos = []
    browse_id = f"VL{playlist_id}"
    client_context = {
        "client": {
            "hl": "en",
            "gl": "US",
            "clientName": "WEB",
            "clientVersion": "2.20241212.03.00"
        }
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    
    def extract_videos_from_renderer(renderer):
        try:
            video_id = renderer.get('videoId')
            if not video_id:
                return None
            title = None
            title_obj = renderer.get('title') or {}
            if isinstance(title_obj, dict):
                runs = title_obj.get('runs') or []
                if runs and isinstance(runs[0], dict):
                    title = runs[0].get('text')
            if not title:
                return None
            duration = 0
            length_obj = renderer.get('lengthSeconds')
            if isinstance(length_obj, str):
                try:
                    duration = int(length_obj)
                except Exception:
                    duration = 0
            elif isinstance(length_obj, (int, float)):
                duration = int(length_obj)
            thumb = None
            thumb_obj = renderer.get('thumbnail') or {}
            thumbs = thumb_obj.get('thumbnails') or []
            if thumbs and isinstance(thumbs[-1], dict):
                thumb = thumbs[-1].get('url')
            if not thumb:
                thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
            return {'title': title, 'url': url, 'thumbnail': thumb, 'duration': duration}
        except Exception:
            return None
    
    def find_videos_and_continuations(contents):
        results = []
        continuations = []
        if not contents:
            return results, continuations
        for c in contents:
            if not isinstance(c, dict):
                continue
            if 'playlistVideoRenderer' in c:
                v = extract_videos_from_renderer(c['playlistVideoRenderer'])
                if v:
                    results.append(v)
                continue
            if 'continuationItemRenderer' in c:
                try:
                    cont = c['continuationItemRenderer']
                    ep = cont.get('continuationEndpoint') or {}
                    cmd = ep.get('continuationCommand') or {}
                    token = cmd.get('token')
                    if token:
                        continuations.append(token)
                except Exception:
                    pass
                continue
            for k in ['itemSectionRenderer', 'playlistVideoListRenderer', 'richGridRenderer', 'sectionListRenderer', 'contents']:
                if k in c:
                    sub = c[k]
                    if isinstance(sub, dict):
                        cont_sub = sub.get('contents')
                        if isinstance(cont_sub, list):
                            r2, ct2 = find_videos_and_continuations(cont_sub)
                            results.extend(r2)
                            continuations.extend(ct2)
                    elif isinstance(sub, list):
                        r2, ct2 = find_videos_and_continuations(sub)
                        results.extend(r2)
                        continuations.extend(ct2)
            if 'contents' in c and isinstance(c['contents'], list):
                r2, ct2 = find_videos_and_continuations(c['contents'])
                results.extend(r2)
                continuations.extend(ct2)
        return results, continuations
    
    def browse_with_token(token=None):
        payload = {"context": client_context}
        if token:
            payload["continuation"] = token
        else:
            payload["browseId"] = browse_id
        req = urllib.request.Request(
            "https://www.youtube.com/youtubei/v1/browse",
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    
    data = browse_with_token(None)
    contents = []
    try:
        contents = data['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer']['content']['sectionListRenderer']['contents']
    except Exception:
        try:
            contents = data['contents']['singleColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer']['content']['sectionListRenderer']['contents']
        except Exception:
            pass
    all_videos, continuations = find_videos_and_continuations(contents)
    
    if continuations:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(browse_with_token, tok) for tok in continuations]
            for fut in as_completed(futures):
                try:
                    cdata = fut.result()
                    ccontents = []
                    try:
                        ccontents = cdata['onResponseReceivedActions'][0]['appendContinuationItemsAction']['continuationItems']
                    except Exception:
                        try:
                            ccontents = cdata['continuationContents']['playlistVideoListContinuation']['contents']
                        except Exception:
                            pass
                    r2, _ = find_videos_and_continuations(ccontents)
                    all_videos.extend(r2)
                except Exception:
                    continue
    
    seen = set()
    unique = []
    for v in all_videos:
        if v['url'] in seen:
            continue
        seen.add(v['url'])
        unique.append(v)
        if not is_admin and len(unique) >= 20:
            break
    return unique


@app.route("/api/status", methods=["GET"])
def check_status():
    job_id = request.args.get("job_id")
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), NOT_FOUND
    return jsonify(job)

@app.route("/download", methods=["GET"])
def download_audio():
    token = request.args.get("token")
    if not access_manager.has_access(token) or not access_manager.is_valid(token):
        return jsonify(error="Token invalid or expired."), UNAUTHORIZED
    try:
        filename = access_manager.get_audio_file(token)
        return send_from_directory(ABS_DOWNLOADS_PATH, path=filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify(error="File not found on server."), NOT_FOUND

def cleanup_temp_dirs(max_age_hours=3):
    try:
        temp_root = Path(ABS_DOWNLOADS_PATH) / "temp"
        if not temp_root.exists():
            return
        now = time.time()
        for entry in temp_root.iterdir():
            try:
                if entry.is_dir():
                    mtime = entry.stat().st_mtime
                    if now - mtime > max_age_hours * 3600:
                        shutil.rmtree(entry, ignore_errors=True)
            except Exception:
                continue
    except Exception:
        pass


def cleanup_temp_dirs_loop():
    while True:
        cleanup_temp_dirs(3)
        time.sleep(3600)


def process_media_job(job_id, urls, format_type, quality, embed_meta):
    job = jobs[job_id]
    
    # The moment the worker picks up the task, change status from queued to processing
    job['status'] = 'processing'
    job['progress'] = 1  # 1% means it has started

    temp_dir = Path(ABS_DOWNLOADS_PATH) / "temp" / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    opts = {
        'outtmpl': str(temp_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'ignoreerrors': True,
        'nocheckcertificate': True,
        'socket_timeout': 15,
        # --- Speed optimizations ---
        'concurrent_fragment_downloads': 8,  # Split streams into fragments downloaded in parallel (HLS/DASH)
        'retries': 5,                        # Fewer wasted restarts on flaky connections
        'fragment_retries': 5,
        'no_warnings': True,
        'noprogress': True,                  # progress_hooks still fire; just skips console spam
    }

    postprocessors = []
    if format_type == 'mp3':
        opts['format'] = 'bestaudio/best'
        audio_quality = '320' if quality == 'high' else ('192' if quality == 'medium' else '128')
        postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': audio_quality})
    else:
        if quality == 'high':
            opts['format'] = 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        elif quality == 'medium':
            opts['format'] = 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        else:
            opts['format'] = 'bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['merge_output_format'] = 'mp4'

    if embed_meta:
        postprocessors.append({'key': 'FFmpegMetadata'})
        opts['writethumbnail'] = True
        postprocessors.append({'key': 'EmbedThumbnail'})
    
    opts['postprocessors'] = postprocessors

    total_files = len(urls)
    current_file_idx = 0

    def progress_hook(d):
        nonlocal current_file_idx
        if d['status'] == 'finished':
            current_file_idx += 1
        elif d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            file_percent = (downloaded / total) if total > 0 else 0
            overall_percent = ((current_file_idx + file_percent) / total_files) * 100
            job['progress'] = min(99, int(overall_percent))
        # Mid-job disk space check (abort if < 500MB)
        try:
            usage = shutil.disk_usage(ABS_DOWNLOADS_PATH)
            if usage.free < 500 * 1024 * 1024:
                raise Exception("Low disk space during download. Aborting.")
        except Exception as e:
            if "Low disk space" in str(e):
                raise

    opts['progress_hooks'] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download(urls)

        downloaded_files = list(temp_dir.glob("*"))
        
        # Bug Fix: If yt-dlp metadata crashes due to no thumbnail, it leaves the folder empty.
        # This fallback retries the download immediately WITHOUT the thumbnail embedder.
        if not downloaded_files and embed_meta:
            opts['writethumbnail'] = False
            opts['postprocessors'] = [p for p in opts['postprocessors'] if p.get('key') != 'EmbedThumbnail']
            with yt_dlp.YoutubeDL(opts) as ydl_fallback:
                ydl_fallback.download(urls)
            downloaded_files = list(temp_dir.glob("*"))

        if not downloaded_files:
            raise Exception("No valid media files could be downloaded.")

        token = secrets.token_urlsafe(TOKEN_LENGTH)

        if len(urls) == 1:
            final_file = downloaded_files[0]
            dest = Path(ABS_DOWNLOADS_PATH) / final_file.name
            if dest.exists(): dest.unlink()
            shutil.move(str(final_file), str(dest))
            access_manager.add_token(token, final_file.name)
        else:
            zip_filename = f"{job['safe_title'][:50]}_{job_id[:6]}.zip"
            zip_path = Path(ABS_DOWNLOADS_PATH) / zip_filename
            # Create zip by adding files one by one and deleting source to save disk space
            with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in downloaded_files:
                    try:
                        zf.write(str(f), arcname=f.name)
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    except Exception:
                        continue
            access_manager.add_token(token, zip_filename)

        shutil.rmtree(temp_dir, ignore_errors=True)
        job['status'] = 'done'
        job['token'] = token
        job['progress'] = 100

    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
        shutil.rmtree(temp_dir, ignore_errors=True)

def main():
    Path(ABS_DOWNLOADS_PATH).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=access_manager.manage_tokens, daemon=True).start()
    threading.Thread(target=cleanup_temp_dirs_loop, daemon=True).start()
    app.run(debug=True, port=5000)

if __name__ == "__main__":
    main()