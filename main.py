"""
main.py - Advanced YouTube Audio/Video Converter API
Includes fixes for missing thumbnail freezes and socket timeouts.
"""

import secrets
import threading
import shutil
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from uuid import uuid4
import yt_dlp
import access_manager
from constants import *
from concurrent.futures import ThreadPoolExecutor

# Global state to track if public users have unlimited access
global_unlimited_mode = False
app = Flask(__name__, static_folder='static', template_folder='templates')

jobs = {}
analysis_jobs = {}
# Create a strict limit of 3 concurrent workers. 
# Anything beyond this waits in the queue.
executor = ThreadPoolExecutor(max_workers=3)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/api/convert", methods=["POST"])
def start_conversion():
    data = request.json
    urls = data.get("urls", [])
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "high")
    embed_meta = data.get("metadata", False)
    playlist_title = data.get("playlist_title", "YouTube_Media")

    if not urls:
        return jsonify(error="No valid URLs provided"), BAD_REQUEST

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
        ydl_opts = {'extract_flat': True, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': 15}
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                raise Exception("Could not retrieve information. The link may be dead or private.")

            if 'entries' in info:
                entries = list(info['entries'])
                
                # --- NEW: PLAYLIST LIMIT CHECK ---
                if not is_admin and len(entries) > 20:
                    raise Exception("Public playlists are limited to 20 items. Host access has unlimited downloads.")
                
                total_items = len(entries)
                
                if total_items == 0:
                    raise Exception("Playlist is empty or unavailable.")
                    
                videos = []
                total_steps = total_items * 3
                current_step = 0
                
                for idx, e in enumerate(entries):
                    i = idx + 1
                    title = e.get('title')
                    
                    job['message'] = f"Fetching video ({i}/{total_items})"
                    current_step += 1
                    job['progress'] = int((current_step / total_steps) * 100)
                    time.sleep(0.01)
                    
                    if e and e.get('url') and title and title not in ['[Private video]', '[Deleted video]']:
                        
                        # --- NEW: DURATION LIMIT CHECK (per video in playlist) ---
                        duration = e.get('duration', 0)
                        if not is_admin and duration > 1800: # 1800 seconds = 30 minutes
                            # Skip this video if it's too long
                            current_step += 2 
                            job['progress'] = int((current_step / total_steps) * 100)
                            continue

                        job['message'] = f"Processing metadata ({i}/{total_items})"
                        current_step += 1
                        job['progress'] = int((current_step / total_steps) * 100)
                        
                        thumb = e.get('thumbnail')
                        if not thumb and e.get('id'):
                            thumb = f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"
                            
                        job['message'] = f"Calculating size ({i}/{total_items})"
                        current_step += 1
                        job['progress'] = int((current_step / total_steps) * 100)
                        
                        videos.append({
                            'title': title,
                            'url': e.get('url'),
                            'thumbnail': thumb,
                            'duration': duration
                        })
                    else:
                        current_step += 2 
                        job['progress'] = int((current_step / total_steps) * 100)
                        
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
                time.sleep(0.1)
                job['message'] = "Processing metadata (1/1)"
                job['progress'] = 66
                time.sleep(0.1)
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


@app.route("/api/convert", methods=["POST"])
def start_conversion():
    data = request.json
    urls = data.get("urls", [])
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "high")
    embed_meta = data.get("metadata", False)
    playlist_title = data.get("playlist_title", "YouTube_Media")

    if not urls:
        return jsonify(error="No valid URLs provided"), BAD_REQUEST

    job_id = str(uuid4())
    safe_title = "".join(c for c in playlist_title if c.isalnum() or c in " _-").strip() or "Playlist"
    
    jobs[job_id] = {'status': 'processing', 'progress': 0, 'safe_title': safe_title}
    threading.Thread(target=process_media_job, args=(job_id, urls, format_type, quality, embed_meta)).start()
    return jsonify(job_id=job_id)

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
        'socket_timeout': 15
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
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', temp_dir)
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
    app.run(debug=True, port=5000)

if __name__ == "__main__":
    main()