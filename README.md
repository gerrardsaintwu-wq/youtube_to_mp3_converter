# 🎵 YouToMedia

A highly reliable, modern YouTube to MP3/MP4 converter powered by **Python (Flask)**, **`yt-dlp`**, and **FFmpeg**. 

Enter the URL of any public YouTube video or playlist, and the application will extract the media, optionally embed metadata and thumbnails, and provide it as a secure, downloadable file or ZIP archive.

## ✨ Features

* **Advanced Media Extraction:** Reliable downloading using `yt-dlp` to bypass common bot-protection roadblocks.
* **Smart Playlist Support:** Paste a playlist link to instantly fetch all videos. Invalid, private, or deleted videos are automatically filtered out.
* **Live Search & Select:** Filter large playlists with a live search bar and select exactly which videos to download.
* **Auto-Zipping:** Downloading multiple files? The server automatically packages them into a clean `.zip` archive for a single-click download.
* **Format & Quality Control:**
  * **Audio (MP3):** 320 kbps, 192 kbps, or 128 kbps.
  * **Video (MP4):** 1080p, 720p, or 480p.
* **Dynamic File Size Estimation:** Instantly calculates estimated file sizes before you start downloading based on the video duration and selected quality.
* **Rich Metadata:** Automatically embeds the original YouTube title, creator, and thumbnail directly into the ID3 tags of your downloaded files.
* **Quality of Life (QoL):** 
  * Real-time progress bars for analysis and downloading.
  * Safety prompts if you try to close the tab before downloading your converted file.
  * Auto-cleaning backend that scrubs temporary files and expired tokens.

---

## 🚀 Usage Guide

1. **Start the application** locally and open `http://127.0.0.1:5000` in your browser.
2. **Step 1 (Analyze):** Paste your YouTube Video or Playlist link and click **"Analyze Link"**. The app will fetch thumbnails, durations, and filter dead links.
3. **Step 2 (Configure):** 
   * Choose between Audio (MP3) or Video (MP4).
   * Select your desired quality.
   * If it's a playlist, select the specific videos you want.
   * Toggle metadata embedding on/off.
4. **Step 3 (Convert):** Click **"Convert Media"**. Watch the live progress bar as the server downloads and processes your files.
5. **Download:** Once it hits 100%, a green **"Download File"** button will appear. Click it to save your media, then convert another!

---

## 📦 Installation & Setup

This application completely replaces the older Node.js implementation with a robust Python backend. 

### 1. System Requirements
You **must** have FFmpeg installed on your system for audio/video conversion to work.
* **Windows:** Install via Winget (`winget install Gyan.FFmpeg`) or download manually.
* **macOS:** `brew install ffmpeg`
* **Ubuntu/Debian:** `sudo apt install ffmpeg`

### 2. Install Python Dependencies
Ensure you have Python 3.8+ installed, then run:
```bash
pip install -r requirements.txt
```

---

## 🖥️ Running the Server (Demo Only)

Use `start_server.bat` in the project root to launch everything:

* Checks that **Python** and **FFmpeg** are on your `PATH` (with clear errors if they aren't).
* Auto-installs Python dependencies on first run if missing.
* Starts the production server (`serve.py` via **Waitress**) in its own console window on `http://localhost:5000`.
* Launches **ngrok** (`ngrok http --url=<your-static-url> 5000`) in the main window, so the app is reachable from outside your network via the static domain. Closing/Ctrl+C-ing ngrok also shuts the server window down.

Note that only one instance should run at a time, since port `5000` is hardcoded in `serve.py`.

> ⚠️ **Demo purposes only.** This setup (single Waitress process, in-memory token store, localhost-only admin endpoint, hardcoded port/token expiry) is fine for personal use and quick demos, **not** optimized for a full web deployment. Exposing it publicly means trusting a token gate with no user accounts, no rate limiting, and no HTTPS management beyond ngrok's edge. Decide critically before hosting anything long-term.