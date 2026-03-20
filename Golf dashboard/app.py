#!/usr/bin/env python3
"""
app.py — Golf Analytics Dashboard (multi-user Flask server)
───────────────────────────────────────────────────────────
Run (development):
    python3 app.py

Run (production):
    gunicorn --workers 3 --bind unix:/run/golf.sock --timeout 180 app:app

Environment (.env or export):
    SECRET_KEY         — 64-char random hex (required in production)
    ANTHROPIC_API_KEY  — Anthropic API key (required for image analysis + coach)
    DATABASE_PATH      — Path to SQLite file (default: ./data/golf.db)
    FLASK_ENV          — production | development
"""

import json, os, urllib.request, urllib.error, hashlib, smtplib, secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from functools import wraps

# Load .env before anything else so DATABASE_PATH is set before db.py is imported
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=False)

from flask import (
    Flask, request, session, jsonify,
    send_file, redirect, url_for
)
from werkzeug.security import generate_password_hash, check_password_hash

from db import init_db, get_db, round_to_dict, session_to_dict, is_duplicate_round, is_duplicate_session

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")          # AI Coach (Pro)
CLAUDE_VISION_MODEL = os.getenv("CLAUDE_VISION_MODEL", "claude-haiku-4-5") # Image analysis
# Free trial uses Haiku: cost-efficient (~10x cheaper than Opus), capable for basic coaching
# with limited data (≤3 rounds). Sonnet is an option if conversion rate needs boosting.
CLAUDE_TRIAL_MODEL  = os.getenv("CLAUDE_TRIAL_MODEL", "claude-haiku-4-5")  # Free trial coach
API_URL       = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Plan limits ───────────────────────────────────────────────────────────────
FREE_ROUNDS_LIMIT           = 3
FREE_RANGE_LIMIT            = 1
FREE_DAILY_AI_ANALYSES      = 5    # image analyses per day (free)
STANDARD_ROUNDS_LIMIT       = 20
STANDARD_RANGE_LIMIT        = 10
STANDARD_DAILY_AI_ANALYSES  = 15
STANDARD_DAILY_COACH_MSGS   = 20   # Golf God chats (standard: 1 week only)
PRO_DAILY_AI_ANALYSES       = 30   # image analyses per day (pro)
PRO_DAILY_COACH_MSGS        = 100  # coach messages per day (pro)

# ── Email config (SMTP) ────────────────────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM    = os.getenv("EMAIL_FROM", SMTP_USER)
APP_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:5000")

# ── Model cost table (USD per million tokens) ─────────────────────────────────
MODEL_COSTS = {
    "claude-haiku-4-5":           {"input": 0.80,  "output": 4.00},
    "claude-3-5-haiku-20241022":  {"input": 0.80,  "output": 4.00},
    "claude-opus-4-5":            {"input": 15.00, "output": 75.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00,  "output": 15.00},
}

def get_setting(key: str, default: str = "") -> str:
    """Read a persistent setting from the DB, falling back to default."""
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()
    except Exception:
        return default


app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv("SECRET_KEY", "dev-insecure-change-me-in-production")
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600  # 30 days

# ── Prompts (copied verbatim from golf_server.py) ────────────────────────────
ANALYSIS_PROMPT = """
You are analysing screenshots from the Virtual Golf 3 / Trackman golf simulator app.
The images may contain a round summary screen, a scorecard screen, and/or hole-by-hole shot maps.

Examine ALL provided images carefully and return a single JSON object with this exact schema:

{
  "rounds": [
    {
      "course": "string — course name exactly as shown",
      "date": "YYYY-MM-DD or null if not visible",
      "score_vs_par": integer (e.g. +2 for two over, -3 for three under, 0 for even par),
      "total_strokes": integer,
      "par": integer (total course par — read from scorecard Par row totals, e.g. 72),
      "holes_played": integer (18 or 9 or partial),
      "fairways_hit_pct": integer or null,
      "avg_drive_m": integer or null,
      "longest_drive_m": integer or null,
      "gir_pct": integer or null,
      "scrambling_pct": integer or null,
      "total_putts": integer or null,
      "putts_per_hole": float or null,
      "holes": [
        {
          "number": integer (hole number),
          "par": integer (3, 4, or 5),
          "strokes": integer (player score on this hole),
          "stroke_index": integer or null (difficulty ranking 1–18),
          "distance_m": integer or null (hole length in metres),
          "gir": true or false or null,
          "putts": integer or null,
          "fairway_hit": true or false or null,
          "drive_m": integer or null,
          "shots": [
            {
              "n": integer,
              "club": string,
              "carry_m": integer or null,
              "result": string or null,
              "remaining_m": integer or null
            }
          ]
        }
      ],
      "source_images": []
    }
  ],
  "range_sessions": []
}

CRITICAL SCORING RULES — read carefully:

1. TOTAL STROKES: Read from the "Strokes" field on the round summary screen (e.g. 74).
   Do NOT confuse with "Score" which is handicap-adjusted.

2. COURSE PAR: Read the Par row totals from the scorecard (Out par + In par, e.g. 36+36=72).
   If no scorecard is visible, read from any "Par" label on the summary screen.

3. SCORE VS PAR: ALWAYS calculate as total_strokes - par (e.g. 74 - 72 = +2).
   NEVER use the "Score" value shown on the Trackman/Virtual Golf round summary screen —
   that value (e.g. "-1") is the handicap-adjusted net score, NOT the score vs course par.
   Even if the summary shows "Score: -1", if strokes=74 and par=72, score_vs_par MUST be +2.

4. HOLE SCORES: Read each player score from the scorecard table row by row.
   The scorecard typically has: Hole row, Par row, Player row.
   Match each score to its correct hole number. Scorecard may be split into two halves
   (holes 1-9 / Out, then holes 10-18 / In). Read all visible holes.
   Boxes around a score = bogey or worse. Filled circles = birdies/eagles.

5. HOLE PAR: Read from the Par row in the scorecard for each corresponding hole column.

- Group hole-map images with their corresponding round summary into ONE round entry.
- If you see two different courses, create two separate round entries.
- Extract hole-level data from scorecard and hole map images when visible. Leave holes: [] only if no scorecard or hole maps are provided.
- GIR = true if the player reached the green in (par - 2) strokes or fewer before putting.
- fairway_hit and drive_m apply to par 4 and par 5 holes only; use null for par 3s.
- Shot extraction: numbered orange circles on the map show each shot. Each label reads "<n> <Club> • <distance>m". Use canonical club names (Dr, 7I, PW). Leave shots: [] if no shot labels are visible.
- SELF-CHECK: Before returning, verify that the sum of all hole strokes equals total_strokes. If it does not, re-read the mismatched holes. A box around a score means bogey (par+1 or worse) — do not read it as the par value.
- Return ONLY the JSON object — no explanation, no markdown fences.
- If a field is genuinely not visible, use null.
"""

RANGE_PROMPT = """
You are analysing screenshots from a launch-monitor range app (e.g. Garmin Golf, Golf Pad),
a golf simulator range mode, or photos from a real driving range session.

Compute session averages from all visible shot rows. Extract all club data and return a single JSON:

{
  "range_sessions": [
    {
      "date": "YYYY-MM-DD or null",
      "club": "canonical short form: Dr, 3W/5W/7W, 2I-9I, PW/GW/AW, or degree like 52 deg",
      "shots": integer or null,
      "avg_carry_m": integer or null,
      "avg_total_m": integer or null,
      "max_carry_m": integer or null,
      "min_carry_m": integer or null,
      "dispersion_m": integer or null,
      "target_hit_pct": integer or null,
      "avg_from_pin_m": float or null,
      "avg_carry_side_m": float or null,
      "dominant_shape": "hook|draw|straight|fade|slice or null",
      "avg_face_angle": float or null,
      "avg_club_path": float or null,
      "avg_face_to_path": float or null,
      "avg_spin_axis_deg": float or null,
      "avg_spin_rate_rpm": integer or null,
      "avg_smash_factor": float or null,
      "avg_club_speed_mph": float or null,
      "avg_ball_speed_mph": float or null,
      "avg_attack_angle_deg": float or null,
      "avg_launch_angle_deg": float or null,
      "source_images": []
    }
  ]
}

Rules:
- Create one entry per club. If multiple clubs appear, create multiple entries.
- DATE: Actively scan every part of the screenshot for a date. Only return null if genuinely not visible.
- Return ONLY the JSON object — no markdown fences, no explanation.
- Use null for any field not visible in the screenshots.
"""

REAL_ROUND_PROMPT = """
You are analysing a photo or screenshot of a golf scorecard, round summary screen,
or scoring app from a real golf course (not a simulator).

Extract ALL visible data and return a single JSON object:

{
  "rounds": [
    {
      "course": "course name as shown",
      "date": "YYYY-MM-DD or null",
      "score_vs_par": integer,
      "total_strokes": integer,
      "par": integer,
      "holes_played": integer,
      "fairways_hit_pct": integer or null,
      "avg_drive_m": integer or null,
      "longest_drive_m": integer or null,
      "gir_pct": integer or null,
      "scrambling_pct": integer or null,
      "total_putts": integer or null,
      "putts_per_hole": float or null,
      "holes": [
        {
          "number": integer,
          "par": integer,
          "strokes": integer,
          "stroke_index": integer or null,
          "distance_m": integer or null,
          "gir": true or false or null,
          "putts": integer or null,
          "fairway_hit": true or false or null
        }
      ],
      "source_images": [],
      "source_type": "real"
    }
  ]
}

Rules:
- ALWAYS extract hole-by-hole data when a scorecard table is visible.
- Infer GIR = true when strokes - putts <= par - 2.
- Calculate score_vs_par = total_strokes - par.
- Return ONLY the JSON object — no markdown fences, no explanation.
- Use null for any field not visible in the image.
"""

PROMPTS = {
    "round":      ANALYSIS_PROMPT,
    "real_round": REAL_ROUND_PROMPT,
    "range":      RANGE_PROMPT,
}

COACH_SYSTEM_PROMPT = """You are an expert personal golf coach. \
Communicate like a PGA teaching professional: direct, data-driven, encouraging, and specific. \
Always reference actual numbers when data is available — never give generic advice when specifics exist.

Coaching principles:
- Identify 1–2 highest-leverage improvements, not a laundry list
- Distinguish simulator vs real-course performance where relevant
- Connect range carry data to on-course GIR outcomes when both are available
- Use concrete targets ("aim for 60% GIR") not vague cues ("hit more greens")
- Acknowledge progress explicitly when metrics are improving

Formatting rules:
- **Bold** the single most important takeaway per response
- Keep responses under 280 words unless the player explicitly asks for detail
- Use short paragraphs (2-4 sentences max); bullet points for lists of 3+ items
- Do NOT use markdown headers (## or ###) — this is a chat interface
- Do not repeat the question back to the player
- Always end with a complete sentence — never stop mid-thought"""

TUTORIAL_PROMPT = """You are the Golf Analytics Dashboard onboarding coach. \
Your job is to give a friendly, concise orientation across exactly 4 topics.
You are currently on step {step} of 4.

STEP 1 — Adding Rounds:
Explain that users can upload Virtual Golf 3 simulator screenshots (goes on the Simulator tab), \
real course scorecard photos (Real Course tab), and practice/range sessions (My Bag tab). \
They simply drag and drop up to 40 screenshots onto the upload zone and the AI reads the data \
automatically — no manual entry needed. Each round appears instantly in their history.

STEP 2 — Range & Practice Sessions:
Explain the My Bag tab tracks club-by-club carry distance, consistency, and accuracy. \
Users drop range screenshots from VG3 or a launch monitor app (like Garmin Golf or Golf Pad) \
onto the My Bag drop zone. They can also import a CSV if they have existing data. \
The dashboard shows carry distance trends per club over time so they can see whether \
their distances are improving session by session.

STEP 3 — Dashboard KPIs & Charts:
Explain the 8 KPI tiles at the top of the dashboard: Rounds played, Average score vs par, \
GIR%, Fairways hit%, Scrambling%, Putts per hole, Average drive distance, and Estimated Handicap. \
Each tile shows a trend arrow vs the previous 5 rounds. The radar chart compares their stats \
against handicap benchmarks. The Score Damage section reveals doubles and 3-putts per round — \
these are almost always the biggest scoring drains to address first.

STEP 4 — Using the AI Coach:
Explain that every tab has a "Coach" button that opens a chat with an AI golf coach \
that has full context of their actual data — scores, GIR, carry distances, everything. \
They can ask questions like "What is my biggest weakness?", "Which club should I practice most?", \
or "Compare my last 5 rounds to my previous 5". The coach gives specific, data-driven answers \
based on their real numbers, not generic advice. Invite them to ask a practice question right now.

Rules:
- Keep each topic explanation to 3–5 sentences. Be warm and enthusiastic but concise.
- Always end with a brief question or prompt that invites engagement.
- Never reveal the internal step number to the user (don't say "Step 1 of 4").
- When you have fully explained the current step and the user has acknowledged it or asked a \
  follow-up you have answered, append the exact marker [STEP_COMPLETE] at the very end of your \
  message with no newline before it. The UI uses this to show the "Next Topic" button.
- If the user asks an off-topic golf question, answer it briefly then gently redirect back.
- Do NOT use markdown headers."""

TRIAL_COACH_PROMPT = """You are a friendly, knowledgeable AI Golf Coach. \
The player is on a free 1-day trial of Golf Analytics.

Your goals during this trial:
1. Be genuinely helpful — give real, actionable advice based on their data
2. Show what becomes possible with more data naturally, e.g. "Once you have 10+ rounds I can \
   spot patterns in your scoring under pressure..."
3. Suggest high-value questions they might not think to ask — scoring patterns, \
   club selection tendencies, weaknesses by hole type, handicap trajectory
4. Help them understand what the Pro plan unlocks: unlimited rounds, unlimited coaching, \
   deeper pattern analysis across months of data
5. Towards the end of the conversation, summarise 2-3 specific things you'd love to dig into \
   with them once they have more data — make it personal to their actual numbers

Free plan reminders (mention naturally when relevant, not as a sales pitch):
- They can upload up to 3 rounds and 1 range session on the free plan
- The full AI Coach requires a Standard or Pro subscription after the trial ends today
- Standard is €5.99/month, Pro is €10/month or €108/year (10% off)

Tone: warm, encouraging, like a knowledgeable friend who plays golf. \
Not salesy — focus 90% on golf, 10% on the product when it genuinely helps them.

Player's name: {name}

Formatting rules:
- Keep responses under 220 words
- Use short paragraphs; bullet points for lists
- Do NOT use markdown headers
- Always end with a complete sentence"""


# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Authentication required"}), 401
        conn = get_db()
        try:
            row = conn.execute("SELECT is_admin FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if not row or not row["is_admin"]:
                return jsonify({"error": "Admin access required"}), 403
        finally:
            conn.close()
        return f(*args, **kwargs)
    return decorated


def current_user_id() -> int:
    return session["user_id"]


def get_current_user(conn):
    return conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()


def is_pro(user_row) -> bool:
    """True if user has an active paid plan (or is admin)."""
    if user_row["is_admin"]:
        return True
    return user_row["plan"] in ("pro", "standard")


def is_standard_or_above(user_row) -> bool:
    return user_row["is_admin"] or user_row["plan"] in ("standard", "pro")


def send_email(to_addr: str, subject: str, html_body: str) -> bool:
    """Send an email via SMTP. Returns True on success, False if not configured."""
    if not SMTP_HOST or not SMTP_USER:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = to_addr
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"  ✗ Email send failed: {e}")
        return False


def track_visit(path: str):
    """Record a page visit. Non-blocking — errors are silently ignored."""
    try:
        uid = session.get("user_id")
        raw_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ip_hash = hashlib.sha256(raw_ip.encode()).hexdigest()[:16] if raw_ip else None
        ua = (request.headers.get("User-Agent") or "")[:200]
        conn = get_db()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO page_visits (path, user_id, ip_hash, user_agent) VALUES (?,?,?,?)",
                    (path, uid, ip_hash, ua)
                )
        finally:
            conn.close()
    except Exception:
        pass


def count_today_actions(conn, user_id: int, action_prefix: str) -> int:
    """Count AI usage actions for a user today."""
    today = __import__("datetime").date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM ai_usage WHERE user_id=? AND action LIKE ? AND date(created_at)=?",
        (user_id, action_prefix + "%", today)
    ).fetchone()
    return row["n"] if row else 0


# ── Static assets ─────────────────────────────────────────────────────────────
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_file(BASE_DIR / "static" / filename)


# ── Page routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    track_visit("/")
    if "user_id" not in session:
        return send_file(BASE_DIR / "landing.html")
    return send_file(BASE_DIR / "golf_dashboard.html")


@app.route("/login")
@app.route("/register")
@app.route("/onboarding")
def onboarding_page():
    track_visit(request.path)
    return send_file(BASE_DIR / "onboarding.html")


@app.route("/admin")
def admin_page():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db()
    try:
        row = conn.execute("SELECT is_admin FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not row or not row["is_admin"]:
            return redirect("/")
    finally:
        conn.close()
    return send_file(BASE_DIR / "admin.html")


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(force=True) or {}
    email        = (data.get("email") or "").strip().lower()
    password     = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not display_name:
        return jsonify({"error": "Display name is required"}), 400

    pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
    verify_token = secrets.token_urlsafe(32)
    conn = get_db()
    try:
        with conn:
            conn.execute(
                """INSERT INTO users (email, password_hash, display_name, coach_trial_end,
                                     email_verify_token, email_verified)
                   VALUES (?, ?, ?, datetime('now', '+1 day'), ?, 0)""",
                (email, pw_hash, display_name, verify_token)
            )
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Send verification email (non-blocking — account works even if email fails)
        verify_url = f"{APP_BASE_URL}/auth/verify_email?token={verify_token}"
        send_email(
            email,
            "Verify your CadenceOS email",
            f"""<p>Hi {display_name},</p>
            <p>Please verify your email address by clicking the link below:</p>
            <p><a href="{verify_url}">{verify_url}</a></p>
            <p>This link does not expire — you can verify at any time.</p>
            <p>— The CadenceOS team</p>"""
        )
        session.permanent = True
        session["user_id"] = user["id"]
        return jsonify({
            "success": True,
            "email_verification_sent": bool(SMTP_HOST),
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "onboarding_complete": user["onboarding_complete"],
                "tutorial_step": user["tutorial_step"],
                "plan": user["plan"],
                "email_verified": False,
            }
        })
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "Email already registered"}), 400
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data        = request.get_json(force=True) or {}
    email       = (data.get("email") or "").strip().lower()
    password    = data.get("password") or ""
    remember_me = bool(data.get("remember_me", False))

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify({"error": "Invalid email or password"}), 401
        with conn:
            conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
        session.permanent = remember_me
        session["user_id"] = user["id"]
        return jsonify({
            "success": True,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "onboarding_complete": user["onboarding_complete"],
                "tutorial_step": user["tutorial_step"],
                "plan": user["plan"],
            }
        })
    finally:
        conn.close()


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/auth/verify_email")
def auth_verify_email():
    token = request.args.get("token", "")
    if not token:
        return "Invalid verification link.", 400
    conn = get_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email_verify_token=?", (token,)).fetchone()
        if not user:
            return "Verification link is invalid or already used.", 400
        with conn:
            conn.execute("UPDATE users SET email_verified=1, email_verify_token=NULL WHERE id=?", (user["id"],))
        return redirect("/?verified=1")
    finally:
        conn.close()


@app.route("/auth/resend_verification", methods=["POST"])
@login_required
def auth_resend_verification():
    conn = get_db()
    try:
        user = get_current_user(conn)
        if user["email_verified"]:
            return jsonify({"success": True, "already_verified": True})
        token = secrets.token_urlsafe(32)
        with conn:
            conn.execute("UPDATE users SET email_verify_token=? WHERE id=?", (token, user["id"]))
        verify_url = f"{APP_BASE_URL}/auth/verify_email?token={token}"
        sent = send_email(
            user["email"],
            "Verify your CadenceOS email",
            f"""<p>Hi {user['display_name']},</p>
            <p>Click the link below to verify your email address:</p>
            <p><a href="{verify_url}">{verify_url}</a></p>
            <p>— The CadenceOS team</p>"""
        )
        return jsonify({"success": True, "sent": sent})
    finally:
        conn.close()


@app.route("/auth/forgot_password", methods=["POST"])
def auth_forgot_password():
    data  = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    conn  = get_db()
    try:
        user = conn.execute("SELECT id, display_name FROM users WHERE email=?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            reset_url = f"{APP_BASE_URL}/reset_password?token={token}"
            with conn:
                conn.execute("UPDATE users SET email_verify_token=? WHERE id=?", (token, user["id"]))
            send_email(
                email,
                "Reset your CadenceOS password",
                f"""<p>Hi {user['display_name']},</p>
                <p>Click the link below to reset your password:</p>
                <p><a href="{reset_url}">{reset_url}</a></p>
                <p>If you did not request this, ignore this email.</p>
                <p>— The CadenceOS team</p>"""
            )
    finally:
        conn.close()
    # Always return success to avoid email enumeration
    return jsonify({"success": True})


@app.route("/auth/me")
def auth_me():
    if "user_id" not in session:
        return jsonify({"error": "Not authenticated"}), 401
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not user:
            session.clear()
            return jsonify({"error": "User not found"}), 401
        import datetime as _dt
        trial_end = user["coach_trial_end"]
        pro = is_pro(user)          # True for admins AND plan='pro' users
        trial_active = False
        if not pro and trial_end:   # trial relevant for free and standard users
            try:
                trial_active = _dt.datetime.utcnow() < _dt.datetime.fromisoformat(trial_end)
            except Exception:
                pass
        # Expose effective plan: admins get 'pro' experience regardless of DB plan column
        effective_plan = "pro" if pro else user["plan"]
        return jsonify({
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "handicap_index": user["handicap_index"],
            "home_course": user["home_course"],
            "units": user["units"],
            "onboarding_complete": user["onboarding_complete"],
            "tutorial_step": user["tutorial_step"],
            "plan": effective_plan,
            "is_admin": bool(user["is_admin"]),
            "profile_image": user["profile_image"],
            "coach_trial_active": trial_active,
            "coach_trial_end": trial_end,
            "email_verified": bool(user["email_verified"]),
            "subscription_status": user["subscription_status"],
        })
    finally:
        conn.close()


@app.route("/auth/onboarding", methods=["POST"])
@login_required
def auth_onboarding():
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        with conn:
            conn.execute(
                """UPDATE users
                   SET display_name=?, handicap_index=?, home_course=?, units=?,
                       handedness=?, gender=?, onboarding_complete=1
                   WHERE id=?""",
                (
                    (data.get("display_name") or "").strip(),
                    data.get("handicap_index"),
                    (data.get("home_course") or "").strip(),
                    data.get("units", "metric"),
                    data.get("handedness", "right"),
                    data.get("gender", ""),
                    current_user_id(),
                )
            )
        return jsonify({"success": True})
    finally:
        conn.close()


@app.route("/auth/tutorial_step", methods=["POST"])
@login_required
def auth_tutorial_step():
    data = request.get_json(force=True) or {}
    step = int(data.get("step", 0))
    if step < 0 or step > 5:
        return jsonify({"error": "step must be 0–5"}), 400
    conn = get_db()
    try:
        with conn:
            conn.execute("UPDATE users SET tutorial_step=? WHERE id=?", (step, current_user_id()))
        return jsonify({"success": True, "step": step})
    finally:
        conn.close()


@app.route("/auth/profile", methods=["POST"])
@login_required
def auth_profile():
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        fields, values = [], []
        for col in ("display_name", "home_course", "units"):
            if col in data:
                fields.append(f"{col}=?")
                values.append((data[col] or "").strip())
        if "handicap_index" in data:
            fields.append("handicap_index=?")
            values.append(data["handicap_index"])
        if not fields:
            return jsonify({"success": True})
        values.append(current_user_id())
        with conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
        return jsonify({"success": True})
    finally:
        conn.close()


# ── Data routes ───────────────────────────────────────────────────────────────
@app.route("/api/rounds")
@login_required
def api_rounds():
    uid = current_user_id()
    conn = get_db()
    try:
        rounds = [round_to_dict(r) for r in
                  conn.execute("SELECT * FROM rounds WHERE user_id=? ORDER BY date DESC", (uid,)).fetchall()]
        sessions = [session_to_dict(s) for s in
                    conn.execute("SELECT * FROM range_sessions WHERE user_id=? ORDER BY date DESC, club", (uid,)).fetchall()]
        return jsonify({"rounds": rounds, "range_sessions": sessions})
    finally:
        conn.close()


@app.route("/api/usage")
@login_required
def api_usage():
    """Return current user's usage counts and plan limits."""
    uid  = current_user_id()
    conn = get_db()
    try:
        user          = get_current_user(conn)
        rounds_count  = conn.execute("SELECT COUNT(*) as n FROM rounds WHERE user_id=?", (uid,)).fetchone()["n"]
        range_count   = conn.execute("SELECT COUNT(*) as n FROM range_sessions WHERE user_id=?", (uid,)).fetchone()["n"]
        today_analyse = count_today_actions(conn, uid, "analyse")
        plan          = user["plan"]
        pro           = user["is_admin"] or plan == "pro"
        standard      = plan == "standard"
        return jsonify({
            "plan": plan,
            "is_pro": is_pro(user),
            "is_admin": bool(user["is_admin"]),
            "rounds_count":  rounds_count,
            "range_count":   range_count,
            "today_analyses": today_analyse,
            "limits": {
                "rounds":         None if pro else (STANDARD_ROUNDS_LIMIT if standard else FREE_ROUNDS_LIMIT),
                "range_sessions": None if pro else (STANDARD_RANGE_LIMIT if standard else FREE_RANGE_LIMIT),
                "daily_analyses": PRO_DAILY_AI_ANALYSES if pro else (STANDARD_DAILY_AI_ANALYSES if standard else FREE_DAILY_AI_ANALYSES),
                "coach":          is_pro(user),
            }
        })
    finally:
        conn.close()


@app.route("/api/analyse", methods=["POST"])
@login_required
def api_analyse():
    if not ANTHROPIC_KEY:
        return jsonify({"error": "Server API key not configured — contact the administrator"}), 503

    uid     = current_user_id()
    conn    = get_db()
    try:
        user = get_current_user(conn)
        pro  = is_pro(user)

        # Daily rate limit
        daily_limit = PRO_DAILY_AI_ANALYSES if pro else FREE_DAILY_AI_ANALYSES
        if count_today_actions(conn, uid, "analyse") >= daily_limit:
            return jsonify({
                "error": "Daily analysis limit reached. Upgrade to Pro for more.",
                "upgrade_required": not pro
            }), 429
    finally:
        conn.close()

    payload = request.get_json(force=True) or {}
    images  = payload.get("images", [])

    if not images:
        return jsonify({"error": "No images provided"}), 400

    mode = payload.get("mode", "round")
    if mode == "range":
        img_cap, max_tokens = 60, 8192
    else:
        img_cap, max_tokens = 40, 8192

    content = []
    for img in images[:img_cap]:
        content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": img.get("media_type", "image/jpeg"),
                "data":       img["data"],
            },
        })
        parts = []
        if img.get("name"):         parts.append(f"Filename: {img['name']}")
        if img.get("lastModified"): parts.append(f"File date: {img['lastModified']}")
        if parts:
            content.append({"type": "text", "text": "[" + " | ".join(parts) + "]"})
    prompt = PROMPTS.get(mode, ANALYSIS_PROMPT)
    content.append({"type": "text", "text": prompt})

    vision_model = get_setting("claude_vision_model", CLAUDE_VISION_MODEL)
    body = json.dumps({
        "model":      vision_model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": content}],
    }).encode()

    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )

    sent = min(len(images), img_cap)
    print(f"  → Analyse: user={current_user_id()}, {sent} image(s), mode={mode}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
        text        = result["content"][0]["text"].strip()
        stop_reason = result.get("stop_reason", "")
        usage       = result.get("usage", {})
        input_tok   = usage.get("input_tokens", 0)
        output_tok  = usage.get("output_tokens", 0)
        tokens_used = input_tok + output_tok

        if stop_reason == "max_tokens":
            print(f"  ⚠ Response hit max_tokens — JSON may be truncated")

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in response")
        extracted = json.loads(text[start:end])

        rounds  = extracted.get("rounds", [])
        range_s = extracted.get("range_sessions", [])

        # ── Post-extraction sanity checks for rounds ─────────────────────
        for r in rounds:
            # 1. score_vs_par must always equal total_strokes - par
            ts  = r.get("total_strokes")
            par = r.get("par")
            if ts is not None and par is not None:
                r["score_vs_par"] = ts - par

            # 2. Warn if hole stroke sums don't match total_strokes
            holes = r.get("holes") or []
            if holes and ts is not None:
                hole_sum = sum(h.get("strokes") or 0 for h in holes)
                if abs(hole_sum - ts) > 0:
                    print(f"  ⚠ Hole stroke sum ({hole_sum}) ≠ total_strokes ({ts}) "
                          f"for '{r.get('course')}' — AI may have misread {abs(hole_sum-ts)} hole(s)")

        # Date fallback for range sessions
        if mode == "range" and range_s:
            file_dates = [img.get("lastModified") for img in images[:img_cap] if img.get("lastModified")]
            if file_dates:
                fallback = file_dates[0]
                for s in range_s:
                    if not s.get("date"):
                        s["date"] = fallback

        # Log usage
        _log_usage(current_user_id(), "analyse", tokens_used,
                   model=vision_model, input_tokens=input_tok, output_tokens=output_tok)

        print(f"  ✓ Extracted {len(rounds)} round(s), {len(range_s)} range session(s)")
        return jsonify({"success": True, "data": extracted, "mode": mode})

    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ✗ API error {e.code}: {err[:200]}")
        return jsonify({"error": f"Anthropic API error {e.code}: {err}"}), 500
    except Exception as e:
        print(f"  ✗ Analyse error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/save", methods=["POST"])
@login_required
def api_save():
    payload    = request.get_json(force=True) or {}
    target     = payload.get("target", "rounds")
    new_rounds = payload.get("rounds", [])
    new_range  = payload.get("range_sessions", [])

    items = new_range if target == "range_sessions" else new_rounds
    if not items:
        return jsonify({"error": f"No {target} provided"}), 400

    uid  = current_user_id()
    conn = get_db()

    # ── Plan-based upload limits ─────────────────────────────────────────────
    user = get_current_user(conn)
    full_pro = user["is_admin"] or user["plan"] == "pro"
    standard = user["plan"] == "standard"
    if not full_pro:
        r_limit  = STANDARD_ROUNDS_LIMIT if standard else FREE_ROUNDS_LIMIT
        rs_limit = STANDARD_RANGE_LIMIT  if standard else FREE_RANGE_LIMIT
        if target == "range_sessions":
            existing = conn.execute("SELECT COUNT(*) as n FROM range_sessions WHERE user_id=?", (uid,)).fetchone()["n"]
            if existing >= rs_limit:
                conn.close()
                tier = "Standard" if standard else "Free"
                return jsonify({
                    "error": f"{tier} plan allows {rs_limit} range session(s). Upgrade for more.",
                    "upgrade_required": True,
                    "limit": rs_limit,
                }), 403
        else:
            existing = conn.execute("SELECT COUNT(*) as n FROM rounds WHERE user_id=?", (uid,)).fetchone()["n"]
            if existing >= r_limit:
                conn.close()
                tier = "Standard" if standard else "Free"
                return jsonify({
                    "error": f"{tier} plan allows {r_limit} rounds. Upgrade for more.",
                    "upgrade_required": True,
                    "limit": r_limit,
                }), 403
    try:
        saved, skipped = 0, 0
        with conn:
            if target == "range_sessions":
                for s in items:
                    if is_duplicate_session(conn, uid, s):
                        skipped += 1
                    else:
                        conn.execute(
                            """INSERT INTO range_sessions
                               (user_id, date, club, shots, avg_carry_m, avg_total_m,
                                max_carry_m, min_carry_m, dispersion_m, target_hit_pct,
                                avg_from_pin_m, avg_carry_side_m, dominant_shape,
                                avg_face_angle, avg_club_path, avg_face_to_path,
                                avg_spin_axis_deg, avg_spin_rate_rpm, avg_smash_factor,
                                avg_club_speed_mph, avg_ball_speed_mph,
                                avg_attack_angle_deg, avg_launch_angle_deg,
                                source_images_json)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                uid,
                                s.get("date"),
                                s.get("club", ""),
                                s.get("shots"),
                                s.get("avg_carry_m"),
                                s.get("avg_total_m"),
                                s.get("max_carry_m"),
                                s.get("min_carry_m"),
                                s.get("dispersion_m"),
                                s.get("target_hit_pct"),
                                s.get("avg_from_pin_m"),
                                s.get("avg_carry_side_m"),
                                s.get("dominant_shape"),
                                s.get("avg_face_angle"),
                                s.get("avg_club_path"),
                                s.get("avg_face_to_path"),
                                s.get("avg_spin_axis_deg"),
                                s.get("avg_spin_rate_rpm"),
                                s.get("avg_smash_factor"),
                                s.get("avg_club_speed_mph"),
                                s.get("avg_ball_speed_mph"),
                                s.get("avg_attack_angle_deg"),
                                s.get("avg_launch_angle_deg"),
                                json.dumps(s.get("source_images", [])),
                            )
                        )
                        saved += 1
                total = conn.execute(
                    "SELECT COUNT(*) FROM range_sessions WHERE user_id=?", (uid,)
                ).fetchone()[0]
                print(f"  ✓ Saved {saved} range session(s), skipped {skipped} dupe(s) — total: {total}")
                return jsonify({"success": True, "saved": saved, "skipped": skipped, "total_range_sessions": total})
            else:
                for r in items:
                    if is_duplicate_round(conn, uid, r):
                        skipped += 1
                    else:
                        conn.execute(
                            """INSERT INTO rounds
                               (user_id, course, date, score_vs_par, total_strokes, par,
                                holes_played, fairways_hit_pct, avg_drive_m, longest_drive_m,
                                gir_pct, scrambling_pct, total_putts, putts_per_hole,
                                source_type, holes_json, source_images_json)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                uid,
                                r.get("course", ""),
                                r.get("date"),
                                r.get("score_vs_par"),
                                r.get("total_strokes"),
                                r.get("par"),
                                r.get("holes_played"),
                                r.get("fairways_hit_pct"),
                                r.get("avg_drive_m"),
                                r.get("longest_drive_m"),
                                r.get("gir_pct"),
                                r.get("scrambling_pct"),
                                r.get("total_putts"),
                                r.get("putts_per_hole"),
                                r.get("source_type", "simulator"),
                                json.dumps(r.get("holes", [])),
                                json.dumps(r.get("source_images", [])),
                            )
                        )
                        saved += 1
                total = conn.execute(
                    "SELECT COUNT(*) FROM rounds WHERE user_id=?", (uid,)
                ).fetchone()[0]
                print(f"  ✓ Saved {saved} round(s), skipped {skipped} dupe(s) — total: {total}")
                return jsonify({"success": True, "saved": saved, "skipped": skipped, "total_rounds": total})
    except Exception as e:
        print(f"  ✗ Save error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    payload   = request.get_json(force=True) or {}
    item_type = payload.get("type")        # "round" or "range"
    index     = payload.get("index")       # 0-based index in user's list

    if item_type not in ("round", "range") or not isinstance(index, int):
        return jsonify({"error": "Need type (round|range) and integer index"}), 400

    uid  = current_user_id()
    conn = get_db()
    try:
        table = "rounds" if item_type == "round" else "range_sessions"
        label_col = "course" if item_type == "round" else "club"
        rows = conn.execute(
            f"SELECT id, {label_col}, date FROM {table} WHERE user_id=? ORDER BY created_at DESC",
            (uid,)
        ).fetchall()
        if index < 0 or index >= len(rows):
            return jsonify({"error": f"Index {index} out of range (0–{len(rows)-1})"}), 400
        row_id = rows[index]["id"]
        label  = rows[index][label_col]
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (row_id, uid))
        print(f"  🗑 Deleted {item_type}: {label}")
        return jsonify({"ok": True, "deleted": label})
    finally:
        conn.close()


@app.route("/api/coach", methods=["POST"])
@login_required
def api_coach():
    if not ANTHROPIC_KEY:
        return jsonify({"error": "Server API key not configured — contact the administrator"}), 503

    # Free tier: coach visible but gated — allow 1-day trial
    import datetime as _dt
    uid  = current_user_id()
    conn = get_db()
    coach_model   = get_setting("claude_model", CLAUDE_MODEL)
    trial_mode    = False
    trial_user_name = "Golfer"
    try:
        user = get_current_user(conn)
        pro  = is_pro(user)
        trial_user_name = user["display_name"] or "Golfer"
        plan = user["plan"]
        if not pro:
            # Check trial window — Free = 24h, Standard = 7 days
            trial_end = user["coach_trial_end"]
            trial_active = False
            if trial_end:
                try:
                    trial_active = _dt.datetime.utcnow() < _dt.datetime.fromisoformat(trial_end)
                except Exception:
                    pass
            if not trial_active:
                tier_label = "Standard" if plan == "standard" else "Free"
                return jsonify({
                    "error": f"Your {tier_label} plan Golf God trial has ended. Upgrade to Pro for unlimited access.",
                    "upgrade_required": True,
                }), 403
            # Trial: use cheaper model, cap daily messages
            daily_cap = STANDARD_DAILY_COACH_MSGS if plan == "standard" else 20
            if count_today_actions(conn, uid, "coach") >= daily_cap:
                return jsonify({"error": "Daily Golf God message limit reached. Upgrade to Pro for unlimited access.", "upgrade_required": True}), 429
            coach_model = get_setting("claude_trial_model", CLAUDE_TRIAL_MODEL)
            trial_mode  = True
        else:
            # Pro daily cap
            if count_today_actions(conn, uid, "coach") >= PRO_DAILY_COACH_MSGS:
                return jsonify({"error": "Daily coach message limit reached. Try again tomorrow."}), 429
    finally:
        conn.close()

    payload  = request.get_json(force=True) or {}
    messages = payload.get("messages", [])
    context  = payload.get("context", {})
    mode     = payload.get("mode", "standard")    # "standard" | "tutorial" | "round_comment"
    step     = int(payload.get("step", 1))

    # round_comment is only available to Pro users (already gated above); Pro uses CLAUDE_MODEL
    # If somehow a non-pro reaches here with round_comment mode, treat as standard

    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    messages = list(messages[-20:])

    if mode == "tutorial":
        system_prompt = TUTORIAL_PROMPT.format(step=step)
    elif mode == "round_comment":
        # Brief, punchy round commentary — no data context prepend needed; prompt is concise
        system_prompt = (
            "You are an expert golf coach. The player has just completed a round and you are "
            "reviewing the stats. Give a 2-3 sentence observation: what stands out (good or bad) "
            "vs their recent form? Reference specific numbers. Be direct, no filler, no sales language."
        )
    elif trial_mode:
        system_prompt = TRIAL_COACH_PROMPT.format(name=trial_user_name)
    else:
        system_prompt = COACH_SYSTEM_PROMPT

    # Prepend data context on first user turn (standard + trial modes only)
    if mode not in ("tutorial", "round_comment"):
        ctx_text = context.get("text", "")
        if ctx_text and len(messages) == 1 and messages[0].get("role") == "user":
            messages[0] = {
                "role": "user",
                "content": f"[MY GOLF DATA]\n{ctx_text}\n\n[MY QUESTION]\n{messages[0]['content']}",
            }

    # round_comment is short — cap at 300 tokens
    max_tokens = 300 if mode == "round_comment" else 2048

    body = json.dumps({
        "model":      coach_model,
        "max_tokens": max_tokens,
        "system":     system_prompt,
        "messages":   messages,
    }).encode()

    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )

    ctx_type = context.get("type", mode)
    print(f"  → Coach: user={current_user_id()}, mode={mode}, step={step}, msgs={len(messages)}, ctx={ctx_type}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        reply  = result["content"][0]["text"].strip()
        usage  = result.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        if result.get("stop_reason") == "max_tokens":
            reply += "\n\n*(response cut short — ask me to continue)*"
            print("  ⚠ Coach hit max_tokens")

        coach_in  = result.get("usage", {}).get("input_tokens", 0)
        coach_out = result.get("usage", {}).get("output_tokens", 0)
        _log_usage(current_user_id(), f"coach_{mode}", tokens,
                   model=coach_model, input_tokens=coach_in, output_tokens=coach_out)
        print(f"  ✓ Coach replied ({len(reply)} chars)")
        return jsonify({"success": True, "reply": reply})

    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ✗ Coach API error {e.code}: {err[:200]}")
        return jsonify({"error": f"API error {e.code}: {err}"}), 500
    except Exception as e:
        print(f"  ✗ Coach error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Helpers ───────────────────────────────────────────────────────────────────
def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    costs = MODEL_COSTS.get(model, {"input": 3.00, "output": 15.00})
    return (input_tokens / 1_000_000) * costs["input"] + (output_tokens / 1_000_000) * costs["output"]


def _log_usage(user_id: int, action: str, tokens: int,
               model: str = "", input_tokens: int = 0, output_tokens: int = 0):
    try:
        cost = _calc_cost(model, input_tokens, output_tokens) if model else 0.0
        conn = get_db()
        with conn:
            conn.execute(
                """INSERT INTO ai_usage
                   (user_id, action, model, input_tokens, output_tokens, tokens_used, cost_usd)
                   VALUES (?,?,?,?,?,?,?)""",
                (user_id, action, model, input_tokens, output_tokens, tokens, cost)
            )
        conn.close()
    except Exception:
        pass  # Usage logging failure must never break the main request


# ── Admin API ─────────────────────────────────────────────────────────────────
@app.route("/admin/stats")
@admin_required
def admin_stats():
    conn = get_db()
    try:
        # User counts
        total_users    = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        free_users     = conn.execute("SELECT COUNT(*) as n FROM users WHERE plan='free' AND is_admin=0").fetchone()["n"]
        standard_users = conn.execute("SELECT COUNT(*) as n FROM users WHERE plan='standard'").fetchone()["n"]
        pro_users      = conn.execute("SELECT COUNT(*) as n FROM users WHERE plan='pro'").fetchone()["n"]
        new_this_week  = conn.execute(
            "SELECT COUNT(*) as n FROM users WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()["n"]

        # Revenue (Standard €5.99/mo, Pro €10/mo)
        mrr_eur = pro_users * 10.0 + standard_users * 5.99

        # API costs — this month
        this_month = __import__("datetime").date.today().strftime("%Y-%m")
        cost_row = conn.execute(
            "SELECT SUM(cost_usd) as total FROM ai_usage WHERE strftime('%Y-%m', created_at)=?",
            (this_month,)
        ).fetchone()
        cost_usd_month = round(cost_row["total"] or 0, 4)
        cost_eur_month = round(cost_usd_month * 0.93, 4)  # approximate USD→EUR

        # All-time cost
        cost_all = conn.execute("SELECT SUM(cost_usd) as total FROM ai_usage").fetchone()
        cost_all_eur = round((cost_all["total"] or 0) * 0.93, 4)

        # Profit estimate (MRR - monthly cost)
        profit_eur = round(mrr_eur - cost_eur_month, 2)

        # API requests today
        today = __import__("datetime").date.today().isoformat()
        requests_today = conn.execute(
            "SELECT COUNT(*) as n FROM ai_usage WHERE date(created_at)=?", (today,)
        ).fetchone()["n"]

        # Monthly breakdown (last 6 months)
        monthly = conn.execute("""
            SELECT strftime('%Y-%m', created_at) as month,
                   COUNT(DISTINCT user_id) as active_users,
                   SUM(cost_usd) as cost
            FROM ai_usage
            GROUP BY month ORDER BY month DESC LIMIT 6
        """).fetchall()

        # Cost by model
        by_model = conn.execute("""
            SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cost_usd) as cost, COUNT(*) as calls
            FROM ai_usage WHERE model IS NOT NULL
            GROUP BY model ORDER BY cost DESC
        """).fetchall()

        return jsonify({
            "users": {
                "total": total_users, "free": free_users,
                "standard": standard_users, "pro": pro_users,
                "new_this_week": new_this_week,
            },
            "revenue": {"mrr_eur": mrr_eur, "arr_eur": round(mrr_eur * 12 * 0.9, 2)},
            "costs": {
                "month_usd": cost_usd_month, "month_eur": cost_eur_month,
                "all_time_eur": cost_all_eur,
            },
            "profit": {"month_eur": profit_eur},
            "activity": {"requests_today": requests_today},
            "monthly": [dict(r) for r in monthly],
            "by_model": [dict(r) for r in by_model],
        })
    finally:
        conn.close()


@app.route("/admin/users")
@admin_required
def admin_users():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT u.id, u.email, u.display_name, u.plan, u.is_admin,
                   u.created_at, u.last_login, u.subscription_status,
                   u.subscription_end_date,
                   (SELECT COUNT(*) FROM rounds r WHERE r.user_id=u.id) as rounds_count,
                   (SELECT COUNT(*) FROM range_sessions s WHERE s.user_id=u.id) as range_count,
                   (SELECT SUM(cost_usd) FROM ai_usage a WHERE a.user_id=u.id) as total_cost_usd,
                   (SELECT COUNT(*) FROM ai_usage a WHERE a.user_id=u.id AND date(a.created_at)=date('now')) as today_calls
            FROM users u ORDER BY u.created_at DESC
        """).fetchall()
        return jsonify({"users": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/admin/users/<int:user_id>/plan", methods=["POST"])
@admin_required
def admin_set_plan(user_id):
    data = request.get_json(force=True) or {}
    plan = data.get("plan")
    if plan not in ("free", "standard", "pro"):
        return jsonify({"error": "plan must be 'free', 'standard', or 'pro'"}), 400
    conn = get_db()
    try:
        with conn:
            if plan == "standard":
                # Give Standard users a fresh 7-day Golf God trial from now
                conn.execute(
                    "UPDATE users SET plan=?, coach_trial_end=datetime('now', '+7 days') WHERE id=?",
                    (plan, user_id)
                )
            elif plan == "free":
                # Downgrade: give a 1-day window from now (or 0 if already used)
                conn.execute(
                    "UPDATE users SET plan=?, coach_trial_end=datetime('now', '+1 day') WHERE id=?",
                    (plan, user_id)
                )
            else:
                conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        return jsonify({"success": True, "plan": plan})
    finally:
        conn.close()


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == session["user_id"]:
        return jsonify({"error": "Cannot delete your own admin account"}), 400
    conn = get_db()
    try:
        user = conn.execute("SELECT email, display_name FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        with conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        print(f"  Admin deleted user: {user['email']}")
        return jsonify({"success": True, "deleted": user["email"]})
    finally:
        conn.close()


ALLOWED_MODELS = {
    "claude-haiku-4-5",
    "claude-3-5-haiku-20241022",
    "claude-opus-4-5",
    "claude-3-5-sonnet-20241022",
}

@app.route("/admin/settings", methods=["GET"])
@admin_required
def admin_get_settings():
    return jsonify({
        "claude_model":        get_setting("claude_model",        CLAUDE_MODEL),
        "claude_vision_model": get_setting("claude_vision_model", CLAUDE_VISION_MODEL),
        "claude_trial_model":  get_setting("claude_trial_model",  CLAUDE_TRIAL_MODEL),
    })


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_save_settings():
    data = request.get_json(force=True) or {}
    allowed_keys = {"claude_model", "claude_vision_model", "claude_trial_model"}
    errors = []
    updates = {}
    for key in allowed_keys:
        if key in data:
            val = data[key]
            if val not in ALLOWED_MODELS:
                errors.append(f"Invalid model for {key}: {val}")
            else:
                updates[key] = val
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    conn = get_db()
    try:
        with conn:
            for key, val in updates.items():
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, val),
                )
        return jsonify({"success": True, "updated": updates})
    finally:
        conn.close()


# ── Subscription routes (Stripe stubs) ────────────────────────────────────────
# TODO: Wire up to real Stripe once you have a Stripe account.
# 1. pip install stripe
# 2. Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in .env
# 3. Create products in Stripe Dashboard:
#    - Standard: €5.99/month
#    - Pro: €10/month or €108/year (10% off)
# 4. Set STRIPE_STANDARD_PRICE_ID, STRIPE_MONTHLY_PRICE_ID, STRIPE_YEARLY_PRICE_ID in .env

@app.route("/subscription/checkout", methods=["POST"])
@login_required
def subscription_checkout():
    """Create a Stripe Checkout session and return the redirect URL."""
    import stripe as stripe_lib
    stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
    if not stripe_lib.api_key:
        return jsonify({"error": "Payment not configured — contact support"}), 503

    conn = get_db()
    try:
        user = get_current_user(conn)
        # TODO: Re-enable email verification gate once email sending is configured
        # if not user["email_verified"]:
        #     return jsonify({
        #         "error": "Please verify your email before upgrading — check your inbox.",
        #         "email_unverified": True,
        #     }), 403

        interval = (request.get_json() or {}).get("interval", "month")

        # Look up the price ID from the database (set via admin pricing panel)
        plan_row = conn.execute(
            "SELECT stripe_price_id FROM pricing_plans WHERE interval=? AND is_active=1 ORDER BY sort_order LIMIT 1",
            (interval,)
        ).fetchone()

        if not plan_row or not plan_row["stripe_price_id"]:
            return jsonify({"error": "No Stripe Price ID configured for this plan — set it in the Admin panel"}), 503

        price_id = plan_row["stripe_price_id"]

        session_obj = stripe_lib.checkout.Session.create(
            customer_email=user["email"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=request.host_url + "subscription/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url + "subscription/cancel",
            metadata={"user_id": str(user["id"])},
        )
        return jsonify({"url": session_obj.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/subscription/webhook", methods=["POST"])
def subscription_webhook():
    """Handle Stripe webhook events."""
    import stripe as stripe_lib
    stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        if webhook_secret and sig_header:
            event = stripe_lib.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = stripe_lib.Event.construct_from(
                __import__("json").loads(payload), stripe_lib.api_key
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    conn = get_db()
    try:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session.get("metadata", {}).get("user_id")
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")
            if user_id:
                with conn:
                    conn.execute(
                        """UPDATE users SET plan='pro',
                           stripe_customer_id=?, stripe_subscription_id=?,
                           subscription_status='active'
                           WHERE id=?""",
                        (customer_id, subscription_id, int(user_id)),
                    )

        elif event["type"] == "customer.subscription.deleted":
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            with conn:
                conn.execute(
                    "UPDATE users SET plan='free', subscription_status='cancelled' WHERE stripe_customer_id=?",
                    (customer_id,),
                )

        elif event["type"] == "invoice.payment_failed":
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            with conn:
                conn.execute(
                    "UPDATE users SET subscription_status='past_due' WHERE stripe_customer_id=?",
                    (customer_id,),
                )
    finally:
        conn.close()

    return jsonify({"received": True})


@app.route("/subscription/success")
@login_required
def subscription_success():
    """Verify the completed Stripe session and immediately upgrade the user's plan."""
    import stripe as stripe_lib
    stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
    session_id = request.args.get("session_id")
    if session_id and stripe_lib.api_key:
        try:
            session_obj = stripe_lib.checkout.Session.retrieve(session_id)
            if session_obj.payment_status == "paid":
                conn = get_db()
                try:
                    user_id = session_obj.metadata.get("user_id")
                    if user_id:
                        with conn:
                            conn.execute(
                                """UPDATE users SET plan='pro',
                                   stripe_customer_id=?, stripe_subscription_id=?,
                                   subscription_status='active'
                                   WHERE id=?""",
                                (session_obj.customer, session_obj.subscription, int(user_id)),
                            )
                finally:
                    conn.close()
        except Exception:
            pass
    return redirect("/")


@app.route("/subscription/cancel")
@login_required
def subscription_cancel_redirect():
    """GET — redirect back to dashboard (Stripe cancel URL)."""
    return redirect("/")


@app.route("/subscription/cancel", methods=["POST"])
@login_required
def subscription_cancel_post():
    """POST — cancel the user's active Stripe subscription at period end."""
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
        if not stripe_lib.api_key:
            return jsonify({"error": "Stripe not configured — contact support"}), 503
        conn = get_db()
        try:
            user = get_current_user(conn)
            sub_id = user["stripe_subscription_id"]
            if not sub_id:
                return jsonify({"error": "No active subscription found on this account"}), 400
            stripe_lib.Subscription.modify(sub_id, cancel_at_period_end=True)
            with conn:
                conn.execute(
                    "UPDATE users SET subscription_status='cancelling' WHERE id=?",
                    (user["id"],)
                )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": "Unexpected error: " + str(e)}), 500


# ── Pricing plans (public read + admin CRUD) ───────────────────────────────────

@app.route("/api/pricing")
def public_pricing():
    """Return active pricing plans for landing page and upgrade modal."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM pricing_plans WHERE is_active=1 ORDER BY sort_order, id"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/admin/pricing", methods=["GET"])
@admin_required
def admin_get_pricing():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM pricing_plans ORDER BY sort_order, id").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/admin/pricing", methods=["POST"])
@admin_required
def admin_create_pricing():
    data = request.get_json() or {}
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO pricing_plans
               (name, plan_key, interval, display_price, display_suffix,
                description, stripe_price_id, badge_text, discount_label, sort_order, is_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("name", ""), data.get("plan_key", "pro"),
                data.get("interval", "month"), data.get("display_price", ""),
                data.get("display_suffix", "/ month"), data.get("description", ""),
                data.get("stripe_price_id", ""), data.get("badge_text", ""),
                data.get("discount_label", ""), int(data.get("sort_order", 0)),
                int(data.get("is_active", 1)),
            ),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/admin/pricing/<int:plan_id>", methods=["PUT"])
@admin_required
def admin_update_pricing(plan_id):
    data = request.get_json() or {}
    conn = get_db()
    try:
        conn.execute(
            """UPDATE pricing_plans SET name=?, plan_key=?, interval=?, display_price=?,
               display_suffix=?, description=?, stripe_price_id=?, badge_text=?,
               discount_label=?, sort_order=?, is_active=? WHERE id=?""",
            (
                data.get("name", ""), data.get("plan_key", "pro"),
                data.get("interval", "month"), data.get("display_price", ""),
                data.get("display_suffix", "/ month"), data.get("description", ""),
                data.get("stripe_price_id", ""), data.get("badge_text", ""),
                data.get("discount_label", ""), int(data.get("sort_order", 0)),
                int(data.get("is_active", 1)), plan_id,
            ),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/admin/pricing/<int:plan_id>", methods=["DELETE"])
@admin_required
def admin_delete_pricing(plan_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM pricing_plans WHERE id=?", (plan_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


# ── Profile image ──────────────────────────────────────────────────────────────
@app.route("/auth/profile_image", methods=["POST"])
@login_required
def auth_profile_image():
    """Save (or clear) a base64 profile image for the current user.
    Body: {"image": "data:image/...;base64,..." | null}
    Stores up to ~800KB of base64 text; larger images should be resized client-side first.
    """
    data  = request.get_json(force=True) or {}
    image = data.get("image")  # may be None to clear

    if image is not None:
        # Basic sanity check: must be a data URI starting with data:image/
        if not isinstance(image, str) or not image.startswith("data:image/"):
            return jsonify({"error": "Invalid image format"}), 400
        # Roughly 600KB limit on base64 payload (~450KB raw)
        if len(image) > 800_000:
            return jsonify({"error": "Image too large — resize before uploading"}), 400

    conn = get_db()
    try:
        with conn:
            conn.execute("UPDATE users SET profile_image=? WHERE id=?",
                         (image, current_user_id()))
        return jsonify({"success": True})
    finally:
        conn.close()


# ── Tournaments ────────────────────────────────────────────────────────────────
@app.route("/api/tournaments", methods=["GET"])
@login_required
def api_tournaments_get():
    uid  = current_user_id()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tournaments WHERE user_id=? ORDER BY date DESC, created_at DESC",
            (uid,)
        ).fetchall()
        return jsonify({"tournaments": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/tournaments", methods=["POST"])
@login_required
def api_tournaments_post():
    uid  = current_user_id()
    conn = get_db()
    try:
        user = get_current_user(conn)
        # Tournament logging available to all users (Pro and trial/free)
        data = request.get_json(force=True) or {}

        # Calculate score_vs_par if not supplied but gross/par available
        score_vs_par = data.get("score_vs_par")
        gross = data.get("gross_score")
        par   = 72 if (data.get("holes_played") or 18) == 18 else 36

        if score_vs_par is None and gross is not None:
            score_vs_par = gross - par

        with conn:
            cur = conn.execute(
                """INSERT INTO tournaments
                   (user_id, name, date, course, format, holes_played,
                    gross_score, net_score, score_vs_par, stableford_pts,
                    position, field_size, handicap_used, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid,
                 (data.get("name") or "").strip(),
                 data.get("date"),
                 (data.get("course") or "").strip(),
                 data.get("format", "stroke"),
                 data.get("holes_played", 18),
                 data.get("gross_score"),
                 data.get("net_score"),
                 score_vs_par,
                 data.get("stableford_pts"),
                 data.get("position"),
                 data.get("field_size"),
                 data.get("handicap_used"),
                 (data.get("notes") or "").strip())
            )
        print(f"  ✓ Tournament saved: id={cur.lastrowid} user={uid}")
        return jsonify({"success": True, "id": cur.lastrowid})
    finally:
        conn.close()


@app.route("/api/tournaments/<int:tourn_id>", methods=["DELETE"])
@login_required
def api_tournaments_delete(tourn_id):
    uid  = current_user_id()
    conn = get_db()
    try:
        with conn:
            conn.execute("DELETE FROM tournaments WHERE id=? AND user_id=?", (tourn_id, uid))
        return jsonify({"success": True})
    finally:
        conn.close()


# ── User data score (admin-only, shows data readiness progress) ───────────────
@app.route("/api/user_score")
@login_required
def api_user_score():
    """
    Returns a data-readiness score for the current user broken down by category.
    Only exposed in the UI for admins until the feature is validated.
    """
    uid  = current_user_id()
    conn = get_db()
    try:
        # Simulator rounds
        sim_rounds = conn.execute(
            "SELECT COUNT(*) as n FROM rounds WHERE user_id=? AND source_type='simulator'", (uid,)
        ).fetchone()["n"]
        # Real course rounds
        real_rounds = conn.execute(
            "SELECT COUNT(*) as n FROM rounds WHERE user_id=? AND source_type='real'", (uid,)
        ).fetchone()["n"]
        # Practice sessions
        practice = conn.execute(
            "SELECT COUNT(*) as n FROM range_sessions WHERE user_id=?", (uid,)
        ).fetchone()["n"]
        # Tournaments
        tournaments = conn.execute(
            "SELECT COUNT(*) as n FROM tournaments WHERE user_id=?", (uid,)
        ).fetchone()["n"]

        def score(count, thresholds):
            """Map count → 0-100 score using milestone thresholds."""
            milestones = thresholds  # e.g. [5, 15, 30, 60]
            for i, m in enumerate(milestones):
                if count < m:
                    prev = milestones[i - 1] if i > 0 else 0
                    frac = (count - prev) / (m - prev)
                    return int(((i + frac) / len(milestones)) * 100)
            return 100

        sim_score   = score(sim_rounds,  [3, 10, 25, 50])
        real_score  = score(real_rounds, [2,  8, 20, 40])
        prac_score  = score(practice,    [2,  8, 20, 40])
        tourn_score = score(tournaments, [1,  4, 10, 20])

        overall = int((sim_score * 0.35 + real_score * 0.25 + prac_score * 0.25 + tourn_score * 0.15))

        return jsonify({
            "overall": overall,
            "categories": {
                "simulator":  {"score": sim_score,   "count": sim_rounds,  "label": "Simulator"},
                "real_course":{"score": real_score,  "count": real_rounds, "label": "Real Course"},
                "practice":   {"score": prac_score,  "count": practice,    "label": "Practice"},
                "tournaments":{"score": tourn_score, "count": tournaments,  "label": "Tournaments"},
            }
        })
    finally:
        conn.close()


# ── Release notes ─────────────────────────────────────────────────────────────
@app.route("/api/release_notes")
def api_release_notes():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM release_notes ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return jsonify({"notes": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/release_notes", methods=["POST"])
@admin_required
def api_release_notes_post():
    data = request.get_json(force=True) or {}
    version = (data.get("version") or "").strip()
    title   = (data.get("title") or "").strip()
    body    = (data.get("body") or "").strip()
    if not version or not title:
        return jsonify({"error": "version and title are required"}), 400
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO release_notes (version, title, body) VALUES (?,?,?)",
                (version, title, body)
            )
        return jsonify({"success": True, "id": cur.lastrowid})
    finally:
        conn.close()


# ── Admin visitor stats ────────────────────────────────────────────────────────
@app.route("/admin/visitor_stats")
@admin_required
def admin_visitor_stats():
    conn = get_db()
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        week_ago = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()

        visits_today = conn.execute(
            "SELECT COUNT(*) as n FROM page_visits WHERE date(created_at)=?", (today,)
        ).fetchone()["n"]

        unique_today = conn.execute(
            "SELECT COUNT(DISTINCT ip_hash) as n FROM page_visits WHERE date(created_at)=?", (today,)
        ).fetchone()["n"]

        visits_week = conn.execute(
            "SELECT COUNT(*) as n FROM page_visits WHERE date(created_at)>=?", (week_ago,)
        ).fetchone()["n"]

        unique_week = conn.execute(
            "SELECT COUNT(DISTINCT ip_hash) as n FROM page_visits WHERE date(created_at)>=?", (week_ago,)
        ).fetchone()["n"]

        by_page = conn.execute("""
            SELECT path, COUNT(*) as hits
            FROM page_visits WHERE date(created_at)>=?
            GROUP BY path ORDER BY hits DESC LIMIT 10
        """, (week_ago,)).fetchall()

        daily = conn.execute("""
            SELECT date(created_at) as day, COUNT(*) as visits,
                   COUNT(DISTINCT ip_hash) as uniques
            FROM page_visits WHERE date(created_at)>=?
            GROUP BY day ORDER BY day DESC
        """, (week_ago,)).fetchall()

        return jsonify({
            "today":    {"visits": visits_today, "unique": unique_today},
            "week":     {"visits": visits_week,  "unique": unique_week},
            "by_page":  [dict(r) for r in by_page],
            "daily":    [dict(r) for r in daily],
        })
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 8080))
    print()
    print("  ⛳  Golf Analytics Dashboard (multi-user)")
    print(f"  ─────────────────────────────────────────")
    print(f"  Server:  http://localhost:{port}")
    print(f"  API key: {'✓ configured' if ANTHROPIC_KEY else '✗ NOT SET — set ANTHROPIC_API_KEY in .env'}")
    print(f"  ─────────────────────────────────────────")
    print()
    app.run(host="0.0.0.0", port=port, debug=(os.getenv("FLASK_ENV") != "production"))
