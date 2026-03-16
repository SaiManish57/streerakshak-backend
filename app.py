# =============================================================
#  app.py — STREERAKSHAK Cloud Backend (Railway)
#  Handles: SOS alerts, GPS tracking, emails, tracking page
# =============================================================

import os
import time
import base64
import smtplib
import threading
import logging
from io import BytesIO

from flask import (
    Flask, request, jsonify,
    render_template, abort, send_file
)
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# =============================================================
# LOGGING
# =============================================================

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# =============================================================
# CONFIGURATION
# All values come from Railway environment variables
# Set these in Railway → Your Project → Variables tab
# =============================================================

EMAIL_SENDER    = os.environ.get("EMAIL_SENDER",    "your_email@gmail.com")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD",  "your_app_password")
EMERGENCY_EMAIL = os.environ.get("EMERGENCY_EMAIL", "contact@gmail.com")
POLICE_EMAIL    = os.environ.get("POLICE_EMAIL",    "police@gmail.com")
TRACKING_TOKEN  = os.environ.get("TRACKING_TOKEN",  "saima-safe-2024")
SECRET_KEY      = os.environ.get("SECRET_KEY",      "streerakshak-app-key")
ADMIN_USER      = os.environ.get("ADMIN_USER",      "admin")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD",  "streerakshak2024")
PORT            = int(os.environ.get("PORT",         8080))

# =============================================================
# GLOBAL STATE
# =============================================================

sos_active    = False
state_lock    = threading.Lock()

location_data = {
    "lat":          0.0,
    "lon":          0.0,
    "accuracy":     "unknown",
    "source":       "none",
    "last_updated": None
}
location_lock = threading.Lock()

latest_photo      = None
latest_photo_time = None
photo_lock        = threading.Lock()

sos_log      = []
sos_log_lock = threading.Lock()

# =============================================================
# FLASK APP
# =============================================================

app = Flask(__name__)

# =============================================================
# AUTH DECORATORS
# =============================================================

def require_auth(f):
    """Protect admin routes with Basic Auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if (not auth or
                auth.username != ADMIN_USER or
                auth.password != ADMIN_PASSWORD):
            return ("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="STREERAKSHAK Admin"'})
        return f(*args, **kwargs)
    return decorated

def require_key(f):
    """Protect app API routes with secret key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        data = request.get_json(silent=True) or {}
        if data.get("key") != SECRET_KEY:
            return jsonify({"error": "Unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated

# =============================================================
# EMAIL HELPERS
# =============================================================

def build_email(subject, body, to_addr, photo_b64=None):
    msg            = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if photo_b64:
        try:
            img_bytes = base64.b64decode(photo_b64)
            part      = MIMEBase("image", "jpeg")
            part.set_payload(img_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment; filename=sos_photo.jpg"
            )
            msg.attach(part)
        except Exception as e:
            log.warning(f"Photo attach error: {e}")
    return msg

def send_smtp(msg, to_addr):
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, to_addr, msg.as_string())
        server.quit()
        log.info(f"Email sent → {to_addr}")
    except Exception as e:
        log.error(f"Email failed ({to_addr}): {e}")

def dispatch_alerts(location_url, track_url, stream_url, photo_b64=None):
    """Send SOS emails to emergency contact and police simultaneously."""

    emergency_body = f"""
STREERAKSHAK — EMERGENCY SOS ALERT
====================================

Someone needs your help immediately!

📍 LOCATION (tap to open Google Maps):
{location_url}

🗺️ LIVE TRACKING PAGE (updates every 5 seconds):
{track_url}

📷 LATEST CAMERA SNAPSHOT:
{stream_url}

This is an automated alert from STREERAKSHAK Safety System.
"""

    police_body = f"""
STREERAKSHAK — WOMEN SAFETY ALERT
====================================

IMMEDIATE ASSISTANCE REQUIRED

📍 LOCATION:
{location_url}

🗺️ LIVE TRACKING:
{track_url}

📷 CAMERA:
{stream_url}

Automated alert — STREERAKSHAK Safety System.
"""

    threads = [
        threading.Thread(
            target=send_smtp,
            args=(
                build_email(
                    "🚨 SOS ALERT — STREERAKSHAK",
                    emergency_body, EMERGENCY_EMAIL, photo_b64
                ),
                EMERGENCY_EMAIL
            ),
            daemon=True
        ),
        threading.Thread(
            target=send_smtp,
            args=(
                build_email(
                    "⚠️ WOMEN SAFETY ALERT — Immediate Assistance",
                    police_body, POLICE_EMAIL, photo_b64
                ),
                POLICE_EMAIL
            ),
            daemon=True
        ),
    ]
    for t in threads:
        t.start()

# =============================================================
# APP API ROUTES (key-protected, called by Android app)
# =============================================================

@app.route("/sos", methods=["POST"])
@require_key
def receive_sos():
    """Called by Android app when SOS is triggered."""
    global sos_active, EMERGENCY_EMAIL

    data     = request.get_json()
    lat      = float(data.get("lat",      0.0))
    lon      = float(data.get("lon",      0.0))
    accuracy = float(data.get("accuracy", 0.0))
    photo    = data.get("photo",  None)
    email    = data.get("email",  None)

    # Update location
    with location_lock:
        location_data["lat"]          = lat
        location_data["lon"]          = lon
        location_data["accuracy"]     = f"GPS (±{accuracy:.0f}m)"
        location_data["source"]       = "gps"
        location_data["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Save photo
    if photo:
        with photo_lock:
            global latest_photo, latest_photo_time
            latest_photo      = photo
            latest_photo_time = time.time()

    # Mark active
    with state_lock:
        sos_active = True

    # Log event
    with sos_log_lock:
        sos_log.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lat":  lat,
            "lon":  lon,
            "type": "SOS_TRIGGER"
        })
        if len(sos_log) > 50:
            sos_log.pop()

    # Override emergency email if app sent a custom one
    if email and "@" in email:
        EMERGENCY_EMAIL = email

    # Build URLs
    base         = request.host_url.rstrip("/")
    location_url = (f"https://maps.google.com/?q={lat},{lon}"
                    if lat else "Location unavailable")
    track_url    = f"{base}/track/{TRACKING_TOKEN}"
    stream_url   = f"{base}/stream"

    # Fire emails in background
    threading.Thread(
        target=dispatch_alerts,
        args=(location_url, track_url, stream_url, photo),
        daemon=True
    ).start()

    log.info(f"SOS received: {lat}, {lon}")
    return jsonify({"status": "ok", "track_url": track_url})


@app.route("/cancel_sos", methods=["POST"])
@require_key
def cancel_sos():
    """Called by Android app when SOS is cancelled."""
    global sos_active
    with state_lock:
        sos_active = False
    with sos_log_lock:
        sos_log.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lat":  0, "lon": 0,
            "type": "SOS_CANCELLED"
        })
    log.info("SOS cancelled")
    return jsonify({"status": "cancelled"})


@app.route("/update_gps", methods=["POST"])
@require_key
def update_gps():
    """Called by Android app every ~30 seconds to push live GPS."""
    data = request.get_json()
    lat  = float(data.get("lat",      0.0))
    lon  = float(data.get("lon",      0.0))
    acc  = float(data.get("accuracy", 0.0))

    with location_lock:
        location_data["lat"]          = lat
        location_data["lon"]          = lon
        location_data["accuracy"]     = f"GPS (±{acc:.0f}m)"
        location_data["source"]       = "gps"
        location_data["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({"status": "ok"})


@app.route("/upload_photo", methods=["POST"])
@require_key
def upload_photo():
    """Upload a camera snapshot separately after SOS."""
    global latest_photo, latest_photo_time
    data  = request.get_json()
    photo = data.get("photo", None)
    if photo:
        with photo_lock:
            latest_photo      = photo
            latest_photo_time = time.time()
        return jsonify({"status": "ok"})
    return jsonify({"status": "no_photo"}), 400


@app.route("/status")
def status():
    """Health check — used by app and admin dashboard."""
    with state_lock:
        active = sos_active
    with location_lock:
        loc = dict(location_data)
    return jsonify({
        "system":     "STREERAKSHAK",
        "status":     "active",
        "sos_active": active,
        "location":   loc,
        "time":       time.strftime("%Y-%m-%d %H:%M:%S")
    })

# =============================================================
# STREAM ROUTE — Serves latest phone snapshot
# =============================================================

@app.route("/stream")
def stream():
    """Returns the latest phone camera snapshot as JPEG."""
    with photo_lock:
        photo = latest_photo
    if photo:
        try:
            img_bytes = base64.b64decode(photo)
            return send_file(
                BytesIO(img_bytes),
                mimetype       = "image/jpeg",
                max_age        = 0,
                last_modified  = time.time()
            )
        except Exception as e:
            log.error(f"Stream error: {e}")

    # No photo yet — return placeholder HTML
    return (
        "<div style='font-family:monospace;color:#666;"
        "padding:30px;background:#0a0a12;text-align:center'>"
        "<p style='font-size:2rem'>📵</p>"
        "<p>No snapshot yet.</p>"
        "<p style='font-size:0.8rem'>Appears when SOS is triggered.</p>"
        "</div>",
        200
    )

# =============================================================
# PUBLIC TRACKING ROUTES (token-only, no password)
# Emergency contacts open these links from the SOS email
# =============================================================

@app.route("/track/<token>")
def public_track(token):
    """Tracking page — open on any browser, no login needed."""
    if token != TRACKING_TOKEN:
        abort(403)
    base = request.host_url.rstrip("/")
    return render_template(
        "track.html",
        stream_url = f"{base}/stream",
        public_url = base,
        token      = token
    )


@app.route("/public_location/<token>")
def public_location(token):
    """Live location API polled by the tracking page."""
    if token != TRACKING_TOKEN:
        abort(403)
    with location_lock:
        data = dict(location_data)
    with state_lock:
        data["sos_active"] = sos_active
    return jsonify(data)

# =============================================================
# ADMIN DASHBOARD ROUTES (Basic Auth protected)
# =============================================================

@app.route("/")
@require_auth
def dashboard():
    base = request.host_url.rstrip("/")
    return render_template(
        "dashboard.html",
        stream_url      = f"{base}/stream",
        track_url       = f"{base}/track/{TRACKING_TOKEN}",
        public_url      = base,
        tracking_token  = TRACKING_TOKEN
    )


@app.route("/location")
@require_auth
def location():
    with location_lock:
        return jsonify(dict(location_data))


@app.route("/sos_log")
@require_auth
def get_sos_log():
    with sos_log_lock:
        return jsonify(list(sos_log))

# =============================================================
# ENTRY POINT
# =============================================================

if __name__ == "__main__":
    log.info("=" * 55)
    log.info("  STREERAKSHAK Cloud Backend Starting...")
    log.info(f"  Port          : {PORT}")
    log.info(f"  Tracking token: {TRACKING_TOKEN}")
    log.info("=" * 55)
    app.run(host="0.0.0.0", port=PORT)
