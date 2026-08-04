import os
import time
import uuid
import re
import sqlite3
import threading
from flask import (
    Flask, render_template, request, jsonify, send_file,
    flash, redirect, url_for, make_response, Response, stream_with_context
)
import yt_dlp
import requests

import shutil

# Detect system FFmpeg if available
FFMPEG_PATH = shutil.which('ffmpeg')
if FFMPEG_PATH:
    print(f"[FFmpeg] System FFmpeg detected at: {FFMPEG_PATH}")
else:
    print("[FFmpeg] Operating in zero-dependency progressive mode.")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'savevibe-super-secret-key-2026'

# Directories Setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'downloads')
DB_PATH = os.path.join(BASE_DIR, 'savevibe.db')
COOKIE_FILE = os.path.join(BASE_DIR, 'cookies.txt')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Automatically write YOUTUBE_COOKIES environment variable to cookies.txt if provided on Render
if os.environ.get('YOUTUBE_COOKIES'):
    try:
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(os.environ.get('YOUTUBE_COOKIES').strip())
        print("[Cookies] Successfully loaded YouTube cookies from environment variable.")
    except Exception as e:
        print(f"[Cookies Warning] Could not write environment cookies: {e}")

# File Expiration & Cleanup (15 minutes max age for cloud server storage optimization)
FILE_MAX_AGE_SECONDS = 900
CLEANUP_INTERVAL_SECONDS = 300

def get_db_connection():
    """Returns a SQLite database connection with row factory for dictionary-like access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes SQLite database tables for download history and visitor feedback."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT DEFAULT 'Guest',
            video_title TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def start_background_cleaner():
    """Background daemon thread to auto-delete temporary download files."""
    def cleanup_task():
        while True:
            try:
                now = time.time()
                if os.path.exists(DOWNLOAD_FOLDER):
                    for filename in os.listdir(DOWNLOAD_FOLDER):
                        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
                        if os.path.isfile(filepath):
                            if (now - os.path.getmtime(filepath)) > FILE_MAX_AGE_SECONDS:
                                try:
                                    os.remove(filepath)
                                    print(f"[Cleanup] Removed old download: {filename}")
                                except Exception as err:
                                    print(f"[Cleanup Error] {filename}: {err}")
            except Exception as e:
                print(f"[Cleanup Exception] {e}")
            time.sleep(CLEANUP_INTERVAL_SECONDS)

    thread = threading.Thread(target=cleanup_task, daemon=True)
    thread.start()

start_background_cleaner()

def sanitize_filename(name):
    """Sanitizes strings for safe filename usage."""
    return re.sub(r'[\/*?:"<>|]', "", name).strip().replace(" ", "_")

def get_yt_dlp_options(format_type='mp4', custom_filename=None):
    """
    Builds robust yt-dlp configuration options optimized for Render & cloud deployment anti-bot protection.
    Uses android_vr and tv player clients to bypass 'Sign in to confirm you're not a bot'.
    """
    out_path = os.path.join(DOWNLOAD_FOLDER, custom_filename if custom_filename else '%(title)s.%(ext)s')

    # Player clients ordered by anti-bot bypass priority
    player_clients = ['android_vr', 'tv', 'web_creator', 'ios', 'android', 'mweb']

    if format_type == 'audio':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_path,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'restrictfilenames': True,
            'remote_components': ['ejs:github'],
            'extractor_args': {
                'youtube': {
                    'player_client': player_clients,
                    'player_skip': ['configs']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        }
        if FFMPEG_PATH:
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
    else:
        # Video MP4 configuration
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': out_path,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'restrictfilenames': True,
            'remote_components': ['ejs:github'],
            'extractor_args': {
                'youtube': {
                    'player_client': player_clients,
                    'player_skip': ['configs']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        }
        if FFMPEG_PATH:
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH

    if os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

    return ydl_opts

def log_download_db(title):
    """Logs download title into SQLite DB."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (username, video_title) VALUES (?, ?)",
            ('Guest', title)
        )
        conn.commit()
        conn.close()
    except Exception as db_err:
        print(f"[DB Error] Failed to log download: {db_err}")

# --- ROUTES ---

@app.route('/')
@app.route('/home')
def home():
    """Main SaveVibe Home Page."""
    return render_template('index.html')

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "bc598ae369mshc3fce5d13ae19c1p1ad26ejsn14136bf24b7d")
RAPIDAPI_HOST = "youtube-mp4-mp3-downloader.p.rapidapi.com"

def extract_youtube_id(url_or_id):
    """Extracts 11-character YouTube video ID from any YouTube URL format."""
    if not url_or_id:
        return None
    url_or_id = url_or_id.strip()
    if len(url_or_id) == 11 and re.match(r'^[0-9A-Za-z_-]{11}$', url_or_id):
        return url_or_id
    regex = r"(?:v=|\/([0-9A-Za-z_-]{11}).*|youtu\.be\/|shorts\/|embed\/)([0-9A-Za-z_-]{11})"
    match = re.search(regex, url_or_id)
    if match:
        return match.group(1) or match.group(2)
    return None

def fetch_rapidapi_download_url(video_id, format_type='1080'):
    """
    Uses RapidAPI YouTube Downloader service to fetch direct video or audio download URL.
    Bypasses all cloud server IP blocks permanently.
    """
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json"
    }

    init_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/download"
    
    if format_type == 'audio':
        fmt = 'mp3'
    elif format_type in ['1080', '720', '480', '360']:
        fmt = format_type
    elif format_type == 'mp4':
        fmt = '1080' # Default legacy fallback
    else:
        fmt = '720'

    params = {
        "format": fmt,
        "id": video_id,
        "audioQuality": "128",
        "addInfo": "false",
        "allowExtendedDuration": "false"
    }

    resp = requests.get(init_url, headers=headers, params=params, timeout=15)
    data = resp.json()

    if not data.get('success') or not data.get('progressId'):
        raise ValueError(f"RapidAPI initiation failed: {data}")

    progress_id = data.get('progressId')
    video_title = data.get('title', 'SaveVibe_Media')

    prog_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/progress"
    for _ in range(15):
        time.sleep(1)
        prog_resp = requests.get(prog_url, headers=headers, params={"id": progress_id}, timeout=10)
        prog_data = prog_resp.json()

        if prog_data.get('finished') and prog_data.get('downloadUrl'):
            return prog_data['downloadUrl'], video_title

    raise TimeoutError("RapidAPI conversion timeout.")

@app.route('/download', methods=['POST'])
def download():
    """
    Handles form submission from SaveVibe main page (index.html).
    Render-optimized: Uses RapidAPI primary engine to bypass cloud server IP blocks permanently.
    """
    url = request.form.get('url', '').strip()
    format_type = request.form.get('format', 'mp4').strip().lower()
    download_token = request.form.get('download_token', '')

    if not url:
        flash("Please enter a valid YouTube video URL.", "danger")
        return redirect(url_for('home'))

    video_id = extract_youtube_id(url)

    # Strategy 1: RapidAPI Endpoint (100% Guaranteed on Render)
    if video_id:
        try:
            download_url, video_title = fetch_rapidapi_download_url(video_id, format_type=format_type)
            log_download_db(video_title)

            response = redirect(download_url)
            if download_token:
                response.set_cookie('downloadToken', download_token, path='/')
            return response
        except Exception as rapid_err:
            print(f"[RapidAPI Method Failed] Falling back to yt-dlp: {rapid_err}")

    # Strategy 2: yt-dlp Direct Stream Extraction Fallback
    try:
        ydl_opts = get_yt_dlp_options(format_type=format_type)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info.get('url')

            if not stream_url:
                formats = [f for f in info.get('formats', []) if f.get('url')]
                if formats:
                    stream_url = formats[-1].get('url')

            if not stream_url:
                raise ValueError("Could not resolve media stream URL.")

            video_title = info.get('title', 'SaveVibe_Media')
            log_download_db(video_title)

            response = redirect(stream_url)
            if download_token:
                response.set_cookie('downloadToken', download_token, path='/')
            return response

    except Exception as err:
        print(f"[Direct Stream URL Extraction Error] {err}")
        flash("Unable to download this video. Please verify the URL or try another link.", "danger")
        return redirect(url_for('home'))

@app.route('/about')
def about():
    """About Us Page."""
    return render_template('about.html')

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    """Contact Us Page & Feedback Form Submission."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        message = request.form.get('message', '').strip()

        if name and email and message:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO feedbacks (name, email, message) VALUES (?, ?, ?)",
                    (name, email, message)
                )
                conn.commit()
                conn.close()
                flash("Thank you for reaching out! Your message has been sent successfully.", "success")
            except Exception as db_err:
                print(f"[DB Error] Failed to save feedback: {db_err}")
                flash("Something went wrong while submitting feedback. Please try again.", "danger")
        else:
            flash("Please fill out all required fields.", "warning")

        return redirect(url_for('contact'))

    return render_template('contact.html')

# --- ADMIN PANEL ROUTES ---

@app.route('/admin')
@app.route('/admin_panel')
def admin_panel():
    """Admin Overview Dashboard."""
    conn = get_db_connection()
    cursor = conn.cursor()

    total_downloads = cursor.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    total_feedbacks = cursor.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
    downloads_list = cursor.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT 20").fetchall()

    conn.close()
    return render_template(
        'admin.html',
        total_downloads=total_downloads,
        total_feedbacks=total_feedbacks,
        downloads_list=downloads_list
    )

@app.route('/admin_downloads')
def admin_downloads():
    """Admin All Downloads Page."""
    conn = get_db_connection()
    cursor = conn.cursor()
    downloads_list = cursor.execute("SELECT * FROM downloads ORDER BY id DESC").fetchall()
    conn.close()
    return render_template('admin_downloads.html', downloads_list=downloads_list)

@app.route('/admin_feedbacks')
def admin_feedbacks():
    """Admin Visitor Feedbacks Page."""
    conn = get_db_connection()
    cursor = conn.cursor()
    feedbacks_list = cursor.execute("SELECT * FROM feedbacks ORDER BY id DESC").fetchall()
    conn.close()
    return render_template('admin_feedbacks.html', feedbacks_list=feedbacks_list)

@app.route('/logout')
def logout():
    """Admin Logout."""
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

# --- RESTful API ENDPOINTS ---

@app.route('/api/info', methods=['GET', 'POST'])
def api_info():
    """
    REST API Endpoint: Fetch video metadata and stream formats.
    Accepts URL via JSON body or 'url' query parameter.
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        url = data.get('url', '').strip()
    else:
        url = request.args.get('url', '').strip()

    if not url:
        return jsonify({'error': 'Please provide a valid YouTube URL'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android_creator', 'android_music', 'ios', 'mweb', 'tv_embedded']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'success': True,
                'title': info.get('title'),
                'uploader': info.get('uploader'),
                'duration': info.get('duration'),
                'view_count': info.get('view_count'),
                'thumbnail': info.get('thumbnail'),
                'description': info.get('description', '')[:200]
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/download', methods=['GET', 'POST'])
def api_download():
    """
    REST API Endpoint: Trigger video or audio download via API request.
    Returns chunked stream or direct attachment download.
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        url = data.get('url', '').strip()
        fmt = data.get('format', 'mp4').strip().lower()
    else:
        url = request.args.get('url', '').strip()
        fmt = request.args.get('format', 'mp4').strip().lower()

    if not url:
        return jsonify({'error': 'Please provide a valid YouTube URL'}), 400

    stream_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': 'best[ext=mp4]/best' if fmt == 'mp4' else 'bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['android_creator', 'android_music', 'ios', 'mweb', 'tv_embedded']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(stream_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info.get('url')
            video_title = info.get('title', 'SaveVibe_Media')
            ext = 'mp4' if fmt == 'mp4' else 'mp3'

            if not stream_url:
                return jsonify({'error': 'Failed to extract stream URL'}), 500

            log_download_db(video_title)

            req = requests.get(stream_url, stream=True, timeout=15)
            clean_name = f"{sanitize_filename(video_title)}.{ext}"

            return Response(
                stream_with_context(req.iter_content(chunk_size=65536)),
                content_type=req.headers.get('Content-Type', 'video/mp4' if fmt == 'mp4' else 'audio/mpeg'),
                headers={
                    'Content-Disposition': f'attachment; filename="{clean_name}"',
                    'Content-Length': req.headers.get('Content-Length', '')
                }
            )
    except Exception as e:
        return jsonify({'error': f'Download failed: {str(e)}'}), 500

if __name__ == '__main__':
    print("==========================================================")
    print(" SaveVibe.com YouTube Downloader Server Running!")
    print(" Local Access: http://127.0.0.1:5000")
    print("==========================================================")
    app.run(host='0.0.0.0', port=5000, debug=True)
