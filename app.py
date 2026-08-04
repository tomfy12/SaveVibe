import os
import time
import uuid
import re
import sqlite3
import threading
from flask import (
    Flask, render_template, request, jsonify, send_file,
    flash, redirect, url_for, make_response, Response, stream_with_context, session, abort
)
import yt_dlp
import requests
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timezone, timedelta

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
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
DB_PATH = os.path.join(BASE_DIR, 'savevibe.db')
COOKIE_FILE = os.path.join(BASE_DIR, 'cookies.txt')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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
    """Initializes SQLite database tables for download history, visitor feedback, and users."""
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Run migrations safely for new download history columns
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN username TEXT DEFAULT 'Guest'")
    except sqlite3.OperationalError:
        pass # Column exists
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass # Column exists
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN format TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN file_size TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN status TEXT DEFAULT 'Completed'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN thumbnail TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN os_device TEXT DEFAULT 'Unknown'")
    except sqlite3.OperationalError:
        pass
    
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN profile_picture TEXT")
    except sqlite3.OperationalError:
        pass
    
    # Check for old default admin and new default admin
    old_admin = cursor.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    new_admin = cursor.execute("SELECT * FROM users WHERE username = 'Gokul767'").fetchone()
    
    new_password_hash = generate_password_hash("G@762001")
    
    if old_admin and not new_admin:
        # Migrate old 'admin' account to 'Gokul767'
        cursor.execute(
            "UPDATE users SET username = ?, email = ?, password_hash = ? WHERE username = 'admin'",
            ('Gokul767', 'admin@savevibe.com', new_password_hash)
        )
        print("[DB] Old default admin migrated to Gokul767.")
    elif not old_admin and not new_admin:
        # Create new default admin
        cursor.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ('Gokul767', 'admin@savevibe.com', new_password_hash, 'admin')
        )
        print("[DB] Default admin user Gokul767 created.")
        
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

def parse_os_device(ua_string):
    """Safely parses user-agent string for OS and device brand."""
    if not ua_string:
        return "Unknown Device"
    
    ua_lower = ua_string.lower()
    
    # OS Detection
    os_name = "Unknown"
    if 'android' in ua_lower:
        os_name = "Android"
    elif 'iphone' in ua_lower or 'ipad' in ua_lower:
        os_name = "iOS"
    elif 'windows' in ua_lower:
        os_name = "Windows"
    elif 'mac os' in ua_lower or 'macos' in ua_lower:
        os_name = "macOS"
    elif 'linux' in ua_lower:
        os_name = "Linux"
    elif 'cros' in ua_lower:
        os_name = "ChromeOS"
        
    # Device Detection
    device = "Unknown"
    if os_name in ["Windows", "macOS", "Linux", "ChromeOS"]:
        device = "Desktop"
    elif 'ipad' in ua_lower or 'tablet' in ua_lower:
        device = "Tablet"
    elif 'iphone' in ua_lower:
        device = "iPhone"
    else:
        if 'samsung' in ua_lower or 'sm-' in ua_lower:
            device = "Samsung"
        elif 'xiaomi' in ua_lower or 'mi ' in ua_lower or 'redmi' in ua_lower or 'poco' in ua_lower:
            device = "Xiaomi"
        elif 'vivo' in ua_lower:
            device = "Vivo"
        elif 'oppo' in ua_lower:
            device = "Oppo"
        elif 'oneplus' in ua_lower:
            device = "OnePlus"
        elif 'pixel' in ua_lower:
            device = "Google Pixel"
        elif 'huawei' in ua_lower:
            device = "Huawei"
        elif 'realme' in ua_lower:
            device = "Realme"
        elif 'motorola' in ua_lower or 'moto ' in ua_lower:
            device = "Motorola"
        elif os_name == "Android":
            device = "Mobile"

    if os_name == "Unknown" and device == "Unknown":
        return "Unknown Device"
    if device == "Unknown":
        return os_name
    return f"{os_name} • {device}"

def log_download_db(title, format_type='Unknown', file_size='Unknown', thumbnail=''):
    """Logs download title into SQLite DB with local timezone."""
    try:
        username = session.get('username', 'Guest')
        user_id = session.get('user_id', None)
        
        # Use IST (Asia/Kolkata) explicitly
        ist = timezone(timedelta(hours=5, minutes=30))
        local_time = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Extract OS/Device
        ua_string = request.user_agent.string if request else ""
        os_device = parse_os_device(ua_string)
        
        # We try to insert with new columns. If it fails, fallback to old schema (safety measure).
        try:
            cursor.execute(
                "INSERT INTO downloads (username, video_title, timestamp, user_id, format, file_size, thumbnail, status, os_device) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, title, local_time, user_id, format_type, file_size, thumbnail, 'Completed', os_device)
            )
        except sqlite3.OperationalError:
            # Fallback if migration hasn't completed or some error occurs
            try:
                cursor.execute(
                    "INSERT INTO downloads (username, video_title, timestamp, user_id, format, file_size, thumbnail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (username, title, local_time, user_id, format_type, file_size, thumbnail, 'Completed')
                )
            except sqlite3.OperationalError:
                cursor.execute(
                    "INSERT INTO downloads (username, video_title, timestamp) VALUES (?, ?, ?)",
                    (username, title, local_time)
                )
            
        conn.commit()
        conn.close()
    except Exception as db_err:
        print(f"[DB Error] Failed to log download: {db_err}")

# --- AUTH DECORATORS ---

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session or session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

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
            # Log RapidAPI download (thumbnail/size might not be available)
            log_download_db(title=video_title, format_type=format_type.upper(), file_size='Unknown', thumbnail=f'https://i.ytimg.com/vi/{video_id}/mqdefault.jpg')

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
            thumbnail = info.get('thumbnail', f'https://i.ytimg.com/vi/{video_id}/mqdefault.jpg' if video_id else '')
            # Attempt to grab filesize
            filesize = info.get('filesize') or info.get('filesize_approx')
            filesize_str = f"{round(filesize / (1024 * 1024), 2)} MB" if filesize else "Unknown"
            
            log_download_db(title=video_title, format_type=format_type.upper(), file_size=filesize_str, thumbnail=thumbnail)

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
@admin_required
def admin_panel():
    """Admin Overview Dashboard."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        total_downloads = cursor.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        total_feedbacks = cursor.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
        downloads_list = cursor.execute("SELECT * FROM downloads ORDER BY timestamp DESC, id DESC LIMIT 20").fetchall()

        conn.close()
        return render_template(
            'admin.html',
            total_downloads=total_downloads,
            total_feedbacks=total_feedbacks,
            downloads_list=downloads_list
        )
    except Exception as e:
        print(f"[Admin Panel Error] Failed to load dashboard: {e}")
        flash("An error occurred while loading the Admin Dashboard. Please try again.", "danger")
        return redirect(url_for('home'))

@app.route('/admin_downloads')
@admin_required
def admin_downloads():
    """Admin All Downloads Page."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        downloads_list = cursor.execute("SELECT * FROM downloads ORDER BY timestamp DESC, id DESC").fetchall()
        conn.close()
        return render_template('admin_downloads.html', downloads_list=downloads_list)
    except Exception as e:
        print(f"[Admin Downloads Error] Failed to load downloads: {e}")
        flash("An error occurred while loading the downloads list. Please try again.", "danger")
        return redirect(url_for('admin_panel'))

@app.route('/admin_feedbacks')
@admin_required
def admin_feedbacks():
    """Admin Visitor Feedbacks Page."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        feedbacks_list = cursor.execute("SELECT * FROM feedbacks ORDER BY timestamp DESC, id DESC").fetchall()
        conn.close()
        return render_template('admin_feedbacks.html', feedbacks_list=feedbacks_list)
    except Exception as e:
        print(f"[Admin Feedbacks Error] Failed to load feedbacks: {e}")
        flash("An error occurred while loading visitor feedbacks. Please try again.", "danger")
        return redirect(url_for('admin_panel'))

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        remember = request.form.get('remember') == 'on'
        
        # Determine redirect URL based on form's next input
        next_url = request.form.get('next', url_for('home'))
        if '?' in next_url:
            next_url = next_url.split('?')[0] # Remove old query params
        
        if not username or not password:
            flash("Please enter both username and password.", "danger")
            return redirect(f"{next_url}?auth=login")
            
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['email'] = user['email']
            session['created_at'] = user['created_at']
            
            # Use dictionary access to avoid KeyError if schema migration hasn't loaded in older rows properly
            # sqlite3.Row supports .keys() to safely check
            if 'profile_picture' in user.keys():
                session['profile_picture'] = user['profile_picture']
            else:
                session['profile_picture'] = None
            # Implement remember me by setting session to permanent
            if remember:
                session.permanent = True
            flash("Login Successful!", "success")
            return redirect(next_url)
        else:
            flash("Invalid Username or Password.", "danger")
    return redirect(url_for('home', auth='login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Determine redirect URL based on form's next input
        next_url = request.form.get('next', url_for('home'))
        if '?' in next_url:
            next_url = next_url.split('?')[0]
        
        if not username or not email or not password or not confirm_password:
            flash("Please fill out all fields.", "warning")
            return redirect(f"{next_url}?auth=signup")
            
        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(f"{next_url}?auth=signup")
            
        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return redirect(f"{next_url}?auth=signup")
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        existing_user = cursor.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, email)).fetchone()
        if existing_user:
            conn.close()
            if existing_user['username'] == username:
                flash("Username already exists.", "danger")
            else:
                flash("Email already exists.", "danger")
            return redirect(f"{next_url}?auth=signup")
            
        hashed_password = generate_password_hash(password)
        try:
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, email, hashed_password, 'user')
            )
            conn.commit()
            flash("Account Created Successfully! Please log in.", "success")
            return redirect(f"{next_url}?auth=login")
        except Exception as e:
            print(f"[DB Error] Registration failed: {e}")
            flash("Something went wrong during registration.", "danger")
            return redirect(f"{next_url}?auth=signup")
        finally:
            conn.close()
            
    return redirect(url_for('home', auth='signup'))

@app.route('/logout')
def logout():
    """User Logout."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

@app.route('/profile')
@login_required
def profile():
    """User Profile."""
    try:
        conn = get_db_connection()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not user_row:
            conn.close()
            session.clear()
            return redirect(url_for('login'))
            
        user = dict(user_row)
        
        # Calculate advanced stats via SQL
        stats_row = conn.execute(
            """
            SELECT 
                COUNT(id) as total_downloads,
                SUM(CASE WHEN LOWER(format) LIKE '%mp4%' OR LOWER(format) LIKE '%video%' THEN 1 ELSE 0 END) as mp4_count,
                SUM(CASE WHEN LOWER(format) LIKE '%mp3%' OR LOWER(format) LIKE '%audio%' THEN 1 ELSE 0 END) as mp3_count,
                SUM(CASE WHEN LOWER(format) IN ('720', '1080', '1440', '2160', '720p', '1080p', '1440p', '2160p', '4k', '8k') THEN 1 ELSE 0 END) as hd_count
            FROM downloads WHERE user_id = ?
            """, 
            (user['id'],)
        ).fetchone()

        total_downloads = stats_row['total_downloads'] or 0
        mp4_count = stats_row['mp4_count'] or 0
        mp3_count = stats_row['mp3_count'] or 0
        hd_count = stats_row['hd_count'] or 0

        favorite_format = "None"
        if total_downloads > 0:
            favorite_format = "MP3 Audio" if mp3_count > mp4_count else "MP4 Video"

        stats = {
            'mp4_count': mp4_count,
            'mp3_count': mp3_count,
            'hd_count': hd_count,
            'favorite_format': favorite_format
        }

        # Get latest 5 downloads for the recent history card
        downloads_rows = conn.execute(
            "SELECT * FROM downloads WHERE user_id = ? ORDER BY timestamp DESC, id DESC LIMIT 5", 
            (user['id'],)
        ).fetchall()
        
        recent_downloads = [dict(d) for d in downloads_rows]
        latest_download = recent_downloads[0] if recent_downloads else None

        conn.close()
        
        return render_template('profile.html', user=user, recent_downloads=recent_downloads, total_downloads=total_downloads, latest_download=latest_download, stats=stats)
    except Exception as e:
        print(f"[Profile Error] Failed to load profile: {e}")
        flash("An error occurred while loading your profile.", "danger")
        return redirect(url_for('home'))

@app.route('/history')
@login_required
def history():
    """User Download History."""
    try:
        conn = get_db_connection()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
        if not user_row:
            conn.close()
            session.clear()
            return redirect(url_for('login'))
            
        user = dict(user_row)
        
        # Get only the user's downloads strictly by user_id
        downloads_rows = conn.execute(
            "SELECT * FROM downloads WHERE user_id = ? ORDER BY timestamp DESC, id DESC LIMIT 50", 
            (user['id'],)
        ).fetchall()
        
        downloads = [dict(d) for d in downloads_rows]
        
        conn.close()
        
        return render_template('history.html', user=user, downloads=downloads)
    except Exception as e:
        print(f"[History Error] Failed to load history: {e}")
        flash("An error occurred while loading your history.", "danger")
        return redirect(url_for('home'))

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    """Handles Profile Update and Avatar Uploads."""
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    action = request.form.get('action', '') # check if 'remove_photo'

    if not username or not email:
        flash("Username and Email are required.", "danger")
        return redirect(url_for('profile'))

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if username or email is taken by another user
    existing_user = cursor.execute("SELECT id FROM users WHERE (username = ? OR email = ?) AND id != ?", (username, email, session['user_id'])).fetchone()
    if existing_user:
        flash("Username or Email is already taken.", "danger")
        conn.close()
        return redirect(url_for('profile'))

    # Handle profile picture
    profile_picture = session.get('profile_picture')
    
    if action == 'remove_photo':
        profile_picture = None
    else:
        file = request.files.get('profile_picture')
        if file and file.filename:
            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4().hex}_{filename}"
            filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
            file.save(filepath)
            profile_picture = f"uploads/{unique_filename}"

    try:
        cursor.execute(
            "UPDATE users SET username = ?, email = ?, profile_picture = ? WHERE id = ?",
            (username, email, profile_picture, session['user_id'])
        )
        conn.commit()
        
        # Update session
        session['username'] = username
        session['email'] = email
        session['profile_picture'] = profile_picture
        
        flash("Profile updated successfully!", "success")
    except Exception as e:
        print(f"[DB Error] Failed to update profile: {e}")
        flash("Failed to update profile.", "danger")
    finally:
        conn.close()
        
    return redirect(url_for('profile'))

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

            thumbnail = info.get('thumbnail', '')
            filesize = info.get('filesize') or info.get('filesize_approx')
            filesize_str = f"{round(filesize / (1024 * 1024), 2)} MB" if filesize else "Unknown"

            req = requests.get(stream_url, stream=True, timeout=15)
            if req.status_code != 200:
                raise ValueError(f"Stream URL is inaccessible (HTTP {req.status_code})")
            
            # Log download only after verifying stream is accessible
            log_download_db(title=video_title, format_type=ext.upper(), file_size=filesize_str, thumbnail=thumbnail)

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
