"""
main.py - Advanced YouTube Audio/Video Converter API
Supports background processing, playlists, zip archives, and metadata embedding.
"""

import secrets
import threading
import shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from uuid import uuid4
import yt_dlp
import access_manager
from constants import *

app = Flask(__name__, static_folder='static', template_folder='templates')

# In-memory job tracker for progress bars
jobs = {}

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/api/info", methods=["POST"])
def get_info():
    """Step 1: Analyze URL and return video or playlist info quickly."""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify(error="URL is required"), BAD_REQUEST

    try:
        # extract_flat=True prevents downloading the actual media during the scan, making it instant
        ydl_opts = {'extract_flat': True, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if 'entries' in info:
                videos = [{'title': e.get('title', 'Unknown'), 'url': e.get('url')} for e in info['entries'] if e.get('url')]
                return jsonify(type="playlist", title=info.get('title'), videos=videos)
            else:
                return jsonify(type="video", title=info.get('title'), url=info.get('webpage_url') or url)
    except Exception as e:
        return jsonify(error=str(e)), BAD_REQUEST

@app.route("/api/convert", methods=["POST"])
def start_conversion():
    """Step 2: Start background conversion job."""
    data = request.json
    urls = data.get("urls", [])
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "high")
    embed_meta = data.get("metadata", False)

    if not urls:
        return jsonify(error="No URLs provided"), BAD_REQUEST

    job_id = str(uuid4())
    jobs[job_id] = {'status': 'processing', 'progress': 0}

    # Run actual download in a background thread to prevent browser timeouts
    threading.Thread(target=process_media_job, args=(job_id, urls, format_type, quality, embed_meta)).start()
    
    return jsonify(job_id=job_id)

@app.route("/api/status", methods=["GET"])
def check_status():
    """Step 3: Poll for conversion progress."""
    job_id = request.args.get("job_id")
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), NOT_FOUND
    return jsonify(job)

@app.route("/download", methods=["GET"])
def download_audio():
    """Final Step: Serve the file securely."""
    token = request.args.get("token")
    if not access_manager.has_access(token) or not access_manager.is_valid(token):
        return jsonify(error="Token invalid or expired."), UNAUTHORIZED

    try:
        filename = access_manager.get_audio_file(token)
        return send_from_directory(ABS_DOWNLOADS_PATH, path=filename, as_attachment=True)
    except FileNotFoundError:
        return jsonify(error="File not found on server."), NOT_FOUND

def process_media_job(job_id, urls, format_type, quality, embed_meta):
    """Background task to handle yt-dlp downloading, merging, tagging, and zipping."""
    job = jobs[job_id]
    
    temp_dir = Path(ABS_DOWNLOADS_PATH) / "temp" / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    opts = {
        'outtmpl': str(temp_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'ignoreerrors': True,
        'nocheckcertificate': True
    }

    # Format & Quality Configuration
    postprocessors = []
    if format_type == 'mp3':
        opts['format'] = 'bestaudio/best'
        audio_quality = '192' if quality == 'high' else ('128' if quality == 'medium' else '96')
        postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': audio_quality})
    else:  # mp4
        if quality == 'high':
            opts['format'] = 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        elif quality == 'medium':
            opts['format'] = 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        else:
            opts['format'] = 'bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        opts['merge_output_format'] = 'mp4'

    # Metadata Configuration
    if embed_meta:
        postprocessors.append({'key': 'FFmpegMetadata'})
        opts['writethumbnail'] = True
        postprocessors.append({'key': 'EmbedThumbnail'})
    
    opts['postprocessors'] = postprocessors

    # Progress Tracking Configuration
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
            job['progress'] = min(99, int(overall_percent)) # Save 100% for post-processing finish

    opts['progress_hooks'] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download(urls)

        # File Management (Single file vs Playlist Zip)
        downloaded_files = list(temp_dir.glob("*"))
        if not downloaded_files:
            raise Exception("No files were successfully downloaded.")

        token = secrets.token_urlsafe(TOKEN_LENGTH)

        if len(urls) == 1:
            # Single file logic
            final_file = downloaded_files[0]
            dest = Path(ABS_DOWNLOADS_PATH) / final_file.name
            if dest.exists(): dest.unlink() # overwrite if exists
            shutil.move(str(final_file), str(dest))
            access_manager.add_token(token, final_file.name)
        else:
            # Playlist logic: Zip everything
            zip_filename = f"{job_id}.zip"
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