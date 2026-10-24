"""
main.py - Advanced YouTube Audio/Video Converter API
Includes robust error-handling for dead links, playlist item filtering (skipping deleted/null entries),
and optimized metadata extraction.
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

jobs = {}

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/api/info", methods=["POST"])
def get_info():
    """Step 1: Analyze URL safely, catching dead links and filtering null/deleted entries."""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify(error="URL is required"), BAD_REQUEST

    try:
        ydl_opts = {'extract_flat': True, 'quiet': True, 'nocheckcertificate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            if not info:
                return jsonify(error="Could not retrieve video or playlist information. The link may be dead or private."), BAD_REQUEST

            if 'entries' in info:
                videos = []
                for e in info['entries']:
                    if e and e.get('url') and e.get('title') and e.get('title') != '[Private video]' and e.get('title') != '[Deleted video]':
                        videos.append({
                            'title': e.get('title'),
                            'url': e.get('url'),
                            'thumbnail': e.get('thumbnail') or (e.get('thumbnails', [{}])[-1].get('url') if e.get('thumbnails') else '')
                        })
                
                if not videos:
                    return jsonify(error="The playlist contains no playable or valid videos."), BAD_REQUEST

                return jsonify(
                    type="playlist", 
                    title=info.get('title', 'YouTube Playlist'), 
                    thumbnail=videos[0]['thumbnail'] if videos else '',
                    videos=videos
                )
            else:
                title = info.get('title')
                if not title or title in ['[Private video]', '[Deleted video]']:
                    return jsonify(error="This video is private, deleted, or unavailable."), BAD_REQUEST
                
                return jsonify(
                    type="video", 
                    title=title, 
                    thumbnail=info.get('thumbnail', ''),
                    url=info.get('webpage_url') or url
                )
    except Exception as e:
        error_msg = str(e)
        if "Sign in to confirm" in error_msg or "Private video" in error_msg:
            error_msg = "This content is private or restricted."
        elif "Video unavailable" in error_msg or "Incomplete YouTube ID" in error_msg:
            error_msg = "The link is dead, invalid, or unavailable."
        return jsonify(error=error_msg), BAD_REQUEST

@app.route("/api/convert", methods=["POST"])
def start_conversion():
    """Step 2: Start background conversion job."""
    data = request.json
    urls = data.get("urls", [])
    format_type = data.get("format", "mp3")
    quality = data.get("quality", "high")
    embed_meta = data.get("metadata", False)

    if not urls:
        return jsonify(error="No valid URLs provided"), BAD_REQUEST

    job_id = str(uuid4())
    jobs[job_id] = {'status': 'processing', 'progress': 0}

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

    postprocessors = []
    if format_type == 'mp3':
        opts['format'] = 'bestaudio/best'
        audio_quality = '320' if quality == 'high' else ('192' if quality == 'medium' else '128')
        postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': audio_quality})
    else:  # mp4
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