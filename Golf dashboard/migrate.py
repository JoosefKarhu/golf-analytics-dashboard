"""
migrate.py — One-shot migration of rounds_data.json → SQLite.

Run once before starting app.py for the first time:
    python migrate.py
"""
import json, os, sys, getpass
from pathlib import Path
from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).parent
SOURCE   = BASE_DIR / "rounds_data.json"
SOURCE_BAK = BASE_DIR / "rounds_data.json.bak"

# Bootstrap db path before importing db module
os.environ.setdefault("DATABASE_PATH", str(BASE_DIR / "data" / "golf.db"))

from db import init_db, get_db, is_duplicate_round, is_duplicate_session


def prompt_owner() -> tuple[str, str, str]:
    """Ask for owner email, display name, and password. Returns (email, display_name, password)."""
    print("\n=== Golf Dashboard — Data Migration ===\n")
    print("This script imports your existing rounds_data.json into the new SQLite database.")
    print("You need to create an owner account to associate the data with.\n")

    email = input("Owner email address: ").strip()
    while not email or "@" not in email:
        email = input("  Invalid — please enter a valid email: ").strip()

    name = input("Display name (e.g. your first name): ").strip()
    while not name:
        name = input("  Display name cannot be empty: ").strip()

    pw = getpass.getpass("Password (min 8 chars): ")
    while len(pw) < 8:
        pw = getpass.getpass("  Too short — password must be at least 8 characters: ")

    pw2 = getpass.getpass("Confirm password: ")
    while pw != pw2:
        print("  Passwords don't match.")
        pw  = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Confirm password: ")

    return email, name, pw


def get_or_create_user(conn, email: str, name: str, password: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        print(f"\n  ✓ Found existing user: {email} (id={row['id']})")
        return row["id"]
    pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name, onboarding_complete, plan) VALUES (?,?,?,1,'free')",
        (email, pw_hash, name),
    )
    uid = cursor.lastrowid
    print(f"\n  ✓ Created owner account: {email} (id={uid})")
    return uid


def migrate_rounds(conn, user_id: int, rounds: list) -> tuple[int, int]:
    inserted = skipped = 0
    for r in rounds:
        if is_duplicate_round(conn, user_id, r):
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO rounds
               (user_id, course, date, score_vs_par, total_strokes, par, holes_played,
                fairways_hit_pct, avg_drive_m, longest_drive_m, gir_pct, scrambling_pct,
                total_putts, putts_per_hole, source_type, holes_json, source_images_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
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
            ),
        )
        inserted += 1
    return inserted, skipped


def migrate_sessions(conn, user_id: int, sessions: list) -> tuple[int, int]:
    inserted = skipped = 0
    for s in sessions:
        if is_duplicate_session(conn, user_id, s):
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO range_sessions
               (user_id, date, club, shots, avg_carry_m, avg_total_m, max_carry_m, min_carry_m,
                dispersion_m, target_hit_pct, avg_from_pin_m, avg_carry_side_m, dominant_shape,
                avg_face_angle, avg_club_path, avg_face_to_path, avg_spin_axis_deg,
                avg_spin_rate_rpm, avg_smash_factor, avg_club_speed_mph, avg_ball_speed_mph,
                avg_attack_angle_deg, avg_launch_angle_deg, source_images_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
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
            ),
        )
        inserted += 1
    return inserted, skipped


def main():
    if not SOURCE.exists():
        print(f"No rounds_data.json found at {SOURCE}. Nothing to migrate.")
        sys.exit(0)

    with open(SOURCE) as f:
        data = json.load(f)

    rounds   = data.get("rounds", [])
    sessions = data.get("range_sessions", [])
    print(f"\nFound {len(rounds)} rounds and {len(sessions)} range sessions to migrate.")

    # Initialise DB
    init_db()

    email, name, password = prompt_owner()

    conn = get_db()
    with conn:
        uid = get_or_create_user(conn, email, name, password)
        ri, rs = migrate_rounds(conn, uid, rounds)
        si, ss = migrate_sessions(conn, uid, sessions)

    conn.close()

    print(f"\n  ✓ Rounds:          {ri} inserted, {rs} skipped (duplicates)")
    print(f"  ✓ Range sessions:  {si} inserted, {ss} skipped (duplicates)")

    # Rename source file
    SOURCE.rename(SOURCE_BAK)
    print(f"\n  ✓ Backed up source to {SOURCE_BAK.name}")
    print("\nMigration complete. You can now start the server with:\n    gunicorn app:app\n")


if __name__ == "__main__":
    main()
