# =============================================================
#  app.py — STREERAKSHAK Cloud Backend (Railway)
#  Each user sends their own Gmail credentials with SOS
#  so emails come from THEIR account, not a shared one
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
# Only non-user-specific values live here as env variables
# User email credentials come from the app request payload
# =============================================================

SECRET_KEY      = os.environ.get("SECRET_KEY",      "streerakshak-app-key")
TRACKING_TOKEN  = os.environ.get("TRACKING_TOKEN",  "saima-safe-2024")
ADMIN_USER      = os.environ.get("ADMIN_USER",       "admin")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD",   "streerakshak2024")
PORT            = int(os.environ.get("PORT",          8080))

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

# Track per-session token (so each user's tracking page works)
active_token = TRACKING_TOKEN
token_lock   = threading.Lock()

# =============================================================
# FLASK APP
# =============================================================

app = Flask(__name__)

# =============================================================
# AUTH DECORATORS
# =============================================================

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if (not auth or
                auth.username != ADMIN_USER or
                auth.password != ADMIN_PASSWORD):
            return ("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="STREERAKSHAK"'})
        return f(*args, **kwargs)
    return decorated

def require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        data = request.get_json(silent=True) or {}
        if data.get("key") != SECRET_KEY:
            return jsonify({"error": "Unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated

# =============================================================
# EMAIL — uses credentials from request payload
# =============================================================

def build_email(subject, body, from_addr, to_addr, photo_b64=None):
    msg            = MIMEMultipart()
    msg["From"]    = from_addr
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

def send_smtp(msg, from_addr, password, to_addr):
    """Send using the USER'S own Gmail credentials."""
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, to_addr, msg.as_string())
        server.quit()
        log.info(f"Email sent: {from_addr} → {to_addr}")
    except smtplib.SMTPAuthenticationError:
        log.error(f"Gmail auth failed for {from_addr} — check App Password")
    except Exception as e:
        log.error(f"Email failed ({to_addr}): {e}")

def dispatch_alerts(
    sender_email, sender_password,
    emergency_email, police_email,
    location_url, track_url, stream_url,
    photo_b64=None
):
    """
    Sends SOS emails FROM the user's own Gmail.
    Both emergency contact + police receive simultaneously.
    """
    base_url = request.host_url.rstrip("/") if request else ""

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

    threads = []

    # Always send to emergency contact
    if emergency_email:
        threads.append(threading.Thread(
            target=send_smtp,
            args=(
                build_email(
                    "🚨 SOS ALERT — STREERAKSHAK",
                    emergency_body, sender_email,
                    emergency_email, photo_b64
                ),
                sender_email, sender_password, emergency_email
            ),
            daemon=True
        ))

    # Send to police if provided
    if police_email:
        threads.append(threading.Thread(
            target=send_smtp,
            args=(
                build_email(
                    "⚠️ WOMEN SAFETY ALERT — Immediate Assistance",
                    police_body, sender_email,
                    police_email, photo_b64
                ),
                sender_email, sender_password, police_email
            ),
            daemon=True
        ))

    for t in threads:
        t.start()

# =============================================================
# APP API ROUTES
# =============================================================

@app.route("/sos", methods=["POST"])
@require_key
def receive_sos():
    """
    Called by Android app when SOS is triggered.
    User's Gmail credentials come in the request payload.
    """
    global sos_active, active_token

    data = request.get_json()

    # Location
    lat      = float(data.get("lat",      0.0))
    lon      = float(data.get("lon",      0.0))
    accuracy = float(data.get("accuracy", 0.0))
    photo    = data.get("photo",    None)

    # User's email credentials (from app settings)
    sender_email    = data.get("sender_email",    "")
    sender_password = data.get("sender_password", "")
    emergency_email = data.get("emergency_email", "")
    police_email    = data.get("police_email",    "")

    # Token (can be per-user)
    token = data.get("tracking_token", TRACKING_TOKEN)

    # Validate
    if not sender_email or not sender_password:
        return jsonify({
            "status":  "error",
            "message": "Missing sender email credentials"
        }), 400

    if not emergency_email:
        return jsonify({
            "status":  "error",
            "message": "Missing emergency contact email"
        }), 400

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

    # Mark active + store token
    with state_lock:
        sos_active = True
    with token_lock:
        active_token = token

    # Log event
    with sos_log_lock:
        sos_log.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lat":  lat,
            "lon":  lon,
            "from": sender_email,
            "type": "SOS_TRIGGER"
        })
        if len(sos_log) > 50:
            sos_log.pop()

    # Build URLs
    base         = request.host_url.rstrip("/")
    location_url = (f"https://maps.google.com/?q={lat},{lon}"
                    if lat else "Location unavailable")
    track_url    = f"{base}/track/{token}"
    stream_url   = f"{base}/stream"

    # Fire emails in background
    threading.Thread(
        target=dispatch_alerts,
        args=(
            sender_email, sender_password,
            emergency_email, police_email,
            location_url, track_url, stream_url, photo
        ),
        daemon=True
    ).start()

    log.info(f"SOS: {lat},{lon} | from: {sender_email} | to: {emergency_email}")
    return jsonify({"status": "ok", "track_url": track_url})


@app.route("/test_email", methods=["POST"])
@require_key
def test_email():
    """
    Sends a test email to verify Gmail credentials work.
    Called from Setup screen's 'Send Test Email' button.
    """
    data            = request.get_json()
    sender_email    = data.get("sender_email",    "")
    sender_password = data.get("sender_password", "")
    to_email        = data.get("to_email",        "")

    if not all([sender_email, sender_password, to_email]):
        return jsonify({"success": False, "message": "Missing fields"}), 400

    try:
        body = """
STREERAKSHAK — Test Email
==========================

✅ Your Gmail is configured correctly!
SOS alerts will be sent from this account.

This is a test email from STREERAKSHAK Safety System.
"""
        msg = build_email(
            "✅ STREERAKSHAK — Email Test Successful",
            body, sender_email, to_email
        )
        send_smtp(msg, sender_email, sender_password, to_email)
        return jsonify({"success": True, "message": "Test email sent!"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/cancel_sos", methods=["POST"])
@require_key
def cancel_sos():
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


@app.route("/stream")
def stream():
    with photo_lock:
        photo = latest_photo
    if photo:
        try:
            return send_file(
                BytesIO(base64.b64decode(photo)),
                mimetype      = "image/jpeg",
                max_age       = 0,
                last_modified = time.time()
            )
        except Exception as e:
            log.error(f"Stream error: {e}")
    return (
        "<div style='font-family:monospace;color:#666;padding:30px;"
        "background:#0a0a12;text-align:center'>"
        "<p style='font-size:2rem'>📵</p>"
        "<p>No snapshot yet — appears when SOS is triggered.</p></div>",
        200
    )

# =============================================================
# PUBLIC TRACKING
# =============================================================

@app.route("/track/<token>")
def public_track(token):
    # Accept both the global token and any active user token
    with token_lock:
        current_token = active_token
    if token != TRACKING_TOKEN and token != current_token:
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
    with token_lock:
        current_token = active_token
    if token != TRACKING_TOKEN and token != current_token:
        abort(403)
    with location_lock:
        data = dict(location_data)
    with state_lock:
        data["sos_active"] = sos_active
    return jsonify(data)

# =============================================================
# ADMIN DASHBOARD
# =============================================================

@app.route("/")
@require_auth
def dashboard():
    base = request.host_url.rstrip("/")
    with token_lock:
        token = active_token
    return render_template(
        "dashboard.html",
        stream_url     = f"{base}/stream",
        track_url      = f"{base}/track/{token}",
        public_url     = base,
        tracking_token = token
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
    log.info("  STREERAKSHAK Cloud Backend")
    log.info("  User email credentials sent per-request")
    log.info(f"  Port: {PORT}")
    log.info("=" * 55)
    app.run(host="0.0.0.0", port=PORT)
