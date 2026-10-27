"""
serve.py
Production server for Windows using Waitress.
"""
from waitress import serve
from main import app
import access_manager
import threading
from constants import ABS_DOWNLOADS_PATH
from pathlib import Path

if __name__ == "__main__":
    # Ensure downloads directory exists
    Path(ABS_DOWNLOADS_PATH).mkdir(parents=True, exist_ok=True)
    
    # Start the token manager in the background
    threading.Thread(target=access_manager.manage_tokens, daemon=True).start()
    
    print("🚀 YT Converter Pro is starting via Waitress...")
    print("🌐 Listening on http://0.0.0.0:5000")
    
    # Run the Waitress production server
    serve(app, host='0.0.0.0', port=5000)