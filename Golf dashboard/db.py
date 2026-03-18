"""
db.py — SQLite database schema and connection helpers.
"""
import sqlite3, os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH") or str(BASE_DIR / "data" / "golf.db"))


def get_db():
    """Return a new SQLite connection with row_factory and pragmas applied."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't already exist."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    with conn:
        conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    email                   TEXT    NOT NULL UNIQUE,
    password_hash           TEXT    NOT NULL,
    display_name            TEXT    NOT NULL DEFAULT '',
    handicap_index          REAL    DEFAULT NULL,
    home_course             TEXT    DEFAULT '',
    handedness              TEXT    NOT NULL DEFAULT 'right',
    gender                  TEXT    NOT NULL DEFAULT '',
    units                   TEXT    NOT NULL DEFAULT 'metric',
    onboarding_complete     INTEGER NOT NULL DEFAULT 0,
    tutorial_step           INTEGER NOT NULL DEFAULT 0,
    plan                    TEXT    NOT NULL DEFAULT 'free',
    is_admin                INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id      TEXT    DEFAULT NULL,
    stripe_subscription_id  TEXT    DEFAULT NULL,
    subscription_status     TEXT    DEFAULT NULL,
    subscription_end_date   TEXT    DEFAULT NULL,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login              TEXT    DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course              TEXT    NOT NULL,
    date                TEXT,
    score_vs_par        INTEGER,
    total_strokes       INTEGER,
    par                 INTEGER,
    holes_played        INTEGER,
    fairways_hit_pct    INTEGER,
    avg_drive_m         INTEGER,
    longest_drive_m     INTEGER,
    gir_pct             INTEGER,
    scrambling_pct      INTEGER,
    total_putts         INTEGER,
    putts_per_hole      REAL,
    source_type         TEXT    NOT NULL DEFAULT 'simulator',
    holes_json          TEXT    NOT NULL DEFAULT '[]',
    source_images_json  TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id, date DESC);

CREATE TABLE IF NOT EXISTS range_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date                TEXT,
    club                TEXT    NOT NULL,
    shots               INTEGER,
    avg_carry_m         INTEGER,
    avg_total_m         INTEGER,
    max_carry_m         INTEGER,
    min_carry_m         INTEGER,
    dispersion_m        INTEGER,
    target_hit_pct      INTEGER,
    avg_from_pin_m      REAL,
    avg_carry_side_m    REAL,
    dominant_shape      TEXT,
    avg_face_angle      REAL,
    avg_club_path       REAL,
    avg_face_to_path    REAL,
    avg_spin_axis_deg   REAL,
    avg_spin_rate_rpm   INTEGER,
    avg_smash_factor    REAL,
    avg_club_speed_mph  REAL,
    avg_ball_speed_mph  REAL,
    avg_attack_angle_deg REAL,
    avg_launch_angle_deg REAL,
    source_images_json  TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_range_user ON range_sessions(user_id, date DESC);

CREATE TABLE IF NOT EXISTS ai_usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action         TEXT    NOT NULL,
    model          TEXT    DEFAULT NULL,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    tokens_used    INTEGER DEFAULT 0,
    cost_usd       REAL    DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_usage_user_month ON ai_usage(user_id, created_at);

CREATE TABLE IF NOT EXISTS tournaments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL DEFAULT '',
    date            TEXT,
    course          TEXT    DEFAULT '',
    format          TEXT    NOT NULL DEFAULT 'stroke',
    holes_played    INTEGER DEFAULT 18,
    gross_score     INTEGER DEFAULT NULL,
    net_score       INTEGER DEFAULT NULL,
    score_vs_par    INTEGER DEFAULT NULL,
    stableford_pts  INTEGER DEFAULT NULL,
    position        INTEGER DEFAULT NULL,
    field_size      INTEGER DEFAULT NULL,
    handicap_used   REAL    DEFAULT NULL,
    notes           TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tournaments_user ON tournaments(user_id, date DESC);

CREATE TABLE IF NOT EXISTS page_visits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT    NOT NULL,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ip_hash    TEXT    DEFAULT NULL,
    user_agent TEXT    DEFAULT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_visits_date ON page_visits(created_at DESC);

CREATE TABLE IF NOT EXISTS release_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
""")
        # Add new columns to existing DBs (safe no-ops if already present)
        for sql in [
            "ALTER TABLE users ADD COLUMN handedness TEXT NOT NULL DEFAULT 'right'",
            "ALTER TABLE users ADD COLUMN gender TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN subscription_end_date TEXT DEFAULT NULL",
            "ALTER TABLE ai_usage ADD COLUMN model TEXT DEFAULT NULL",
            "ALTER TABLE ai_usage ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE ai_usage ADD COLUMN output_tokens INTEGER DEFAULT 0",
            "ALTER TABLE ai_usage ADD COLUMN cost_usd REAL DEFAULT 0",
            # coach trial: set to 24h after account creation for free users
            "ALTER TABLE users ADD COLUMN coach_trial_end TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN profile_image TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN email_verify_token TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
        # Ensure the first registered user is always admin
        conn.execute("UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)")
        # Set coach_trial_end for users who don't have it yet (24h from creation)
        conn.execute("""UPDATE users SET coach_trial_end=datetime(created_at, '+1 day')
                        WHERE coach_trial_end IS NULL""")
    conn.close()
    print(f"  ✓ Database ready: {DATABASE_PATH}")


def round_to_dict(row):
    """Convert a rounds DB row to the JSON shape the dashboard expects."""
    import json as _json
    d = dict(row)
    d["holes"]         = _json.loads(d.pop("holes_json", "[]") or "[]")
    d["source_images"] = _json.loads(d.pop("source_images_json", "[]") or "[]")
    # Remove server-only fields
    d.pop("user_id", None)
    d.pop("created_at", None)
    return d


def session_to_dict(row):
    """Convert a range_sessions DB row to the JSON shape the dashboard expects."""
    import json as _json
    d = dict(row)
    d["source_images"] = _json.loads(d.pop("source_images_json", "[]") or "[]")
    d.pop("user_id", None)
    d.pop("created_at", None)
    return d


# ── Duplicate detection (mirrors golf_server.py logic) ──────────────────────

def is_duplicate_round(conn, user_id: int, r: dict) -> bool:
    course  = (r.get("course") or "").strip().lower()
    date    = r.get("date")
    strokes = r.get("total_strokes")
    if not course or not date or strokes is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM rounds WHERE user_id=? AND lower(trim(course))=? AND date=? AND total_strokes=?",
        (user_id, course, date, strokes)
    ).fetchone()
    return row is not None


def is_duplicate_session(conn, user_id: int, s: dict) -> bool:
    club  = (s.get("club") or "").strip().lower()
    date  = s.get("date")
    carry = s.get("avg_carry_m")
    shots = s.get("shots")
    if not club or not date or carry is None:
        return False
    if shots is not None:
        row = conn.execute(
            "SELECT 1 FROM range_sessions WHERE user_id=? AND lower(trim(club))=? AND date=? AND avg_carry_m=? AND (shots IS NULL OR shots=?)",
            (user_id, club, date, carry, shots)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM range_sessions WHERE user_id=? AND lower(trim(club))=? AND date=? AND avg_carry_m=?",
            (user_id, club, date, carry)
        ).fetchone()
    return row is not None


if __name__ == "__main__":
    init_db()
