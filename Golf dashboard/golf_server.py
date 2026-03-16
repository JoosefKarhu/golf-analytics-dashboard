#!/usr/bin/env python3
"""
Golf Dashboard Server v1.1
──────────────────────────────────────────────────────────────────────────────
Lightweight HTTP server that powers the interactive Golf Analytics Dashboard.

  Run:  python3 golf_server.py
  Open: http://localhost:8080

No extra packages needed — uses Python stdlib only.

Endpoints:
  GET  /                  Serves golf_dashboard.html
  GET  /api/rounds        Returns rounds_data.json
  POST /api/analyse       Receives images → calls Anthropic API → returns round data
  POST /api/save          Appends new rounds to rounds_data.json
  POST /api/coach         AI coach chat — sends conversation + data context to Claude
──────────────────────────────────────────────────────────────────────────────
"""

import json, os, sys, urllib.request, urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Config ─────────────────────────────────────────────────────────────────
PORT         = 8080
BASE_DIR     = Path(__file__).parent
DATA_FILE    = BASE_DIR / "rounds_data.json"
HTML_FILE    = BASE_DIR / "golf_dashboard.html"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
API_URL      = "https://api.anthropic.com/v1/messages"

ANALYSIS_PROMPT = """
You are analysing screenshots from the Virtual Golf 3 golf simulator app.
The images may contain hole-by-hole shot maps and/or round summary/scorecard screens.

Examine ALL provided images carefully and return a single JSON object with this exact schema:

{
  "rounds": [
    {
      "course": "string — course name exactly as shown",
      "date": "YYYY-MM-DD or null if not visible",
      "score_vs_par": integer (e.g. 7 for +7, -1 for -1, 0 for E),
      "total_strokes": integer,
      "par": integer (total course par, derive from strokes - score_vs_par),
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
          "gir": true or false or null (reached green in regulation: strokes to reach green <= par - 2),
          "putts": integer or null (number of putts on this hole),
          "fairway_hit": true or false or null (tee shot on fairway — par 4/5 only, null for par 3),
          "drive_m": integer or null (tee shot carry distance in metres — par 4/5 only),
          "shots": [
            {
              "n": integer (shot number starting from 1),
              "club": string (canonical short form: Dr, 3W, 5W, 7W, 2I–9I, PW, GW, AW, or degree like 56deg),
              "carry_m": integer or null (shot distance in metres as shown on the label),
              "result": string or null ("fairway", "rough" (deep/penalising), "playable_rough" (light rough, still manageable), "green", "fringe", "bunker", "penalty"),
              "remaining_m": integer or null (distance to hole remaining after this shot)
            }
          ]
        }
      ],
      "source_images": []
    }
  ],
  "range_sessions": []
}

Rules:
- Group hole-map images with their corresponding round summary into ONE round entry.
- If you see two different courses, create two separate round entries.
- Infer par = total_strokes - score_vs_par.
- Extract hole-level data from hole map images when visible. Leave holes: [] if only a summary screen is provided.
- GIR = true if the player reached the green in (par - 2) strokes or fewer before putting. Infer from shot maps where possible.
- fairway_hit and drive_m apply to par 4 and par 5 holes only; use null for par 3s.
- Shot extraction from hole maps: numbered orange circles on the map show each shot. Each label reads "<n> <Club> • <distance>m" (e.g. "1 Dr • 199m", "2 7i • 114m"). The finish shot "F" shows putts (e.g. "F 2 Putts") — omit it from shots[]. The bottom detail panel shows result location ("199m to rough", "132m to hole") — use these for carry_m, result, and remaining_m. Use canonical club names (Dr not Driver, 7I not 7i, PW not P). Leave shots: [] if no shot labels are visible on the hole map.
- Return ONLY the JSON object — no explanation, no markdown fences.
- If a field is genuinely not visible, use null.
"""

RANGE_PROMPT = """
You are analysing screenshots from a launch-monitor range app (e.g. Garmin Golf, Golf Pad),
a golf simulator range mode, or photos from a real driving range session.

The app shows two swipeable data views per session:
  View 1 columns: Carry, Total, Club Speed (mph), Ball Speed (mph), Smash Factor,
                  Spin Rate (rpm), Spin Axis (deg), Curve (m + L/R), Attack Angle (deg)
  View 2 columns: Face-to-Path (deg), Club Path (deg), Face Angle (deg), Launch Angle (deg),
                  Launch Direction (deg + L/R), Carry Side (m + L/R), Total Side (m),
                  Dynamic Loft (deg), Impact Height (mm), Spin Loft (deg)

Compute session averages from all visible shot rows. Extract all club data and return a single JSON:

{
  "range_sessions": [
    {
      "date": "YYYY-MM-DD or null — look carefully at the app header, navigation bar, date picker, session title, or any visible timestamp in the screenshot",
      "club": "canonical short form: Dr (driver), 3W/5W/7W (fairway woods), 2I-9I (irons), PW/GW/AW (wedges), then degree notation like 52 deg/56 deg/60 deg",
      "shots": integer or null,
      "avg_carry_m": integer or null,
      "avg_total_m": integer or null,
      "max_carry_m": integer or null,
      "min_carry_m": integer or null,
      "dispersion_m": integer or null (lateral spread width in metres — from dispersion ellipse if shown),
      "target_hit_pct": integer or null (% of shots on target — compute from Target Hit count / total shots),
      "avg_from_pin_m": float or null (average distance from pin — may be absent for some clubs),
      "avg_carry_side_m": float or null (average lateral offset in metres — positive=right, negative=left; derive sign from R/L suffix),
      "dominant_shape": "derive from avg Spin Axis: >+9 -> 'hook', +3 to +9 -> 'draw', -2 to +2 -> 'straight', -9 to -3 -> 'fade', <-9 -> 'slice'; gap values (+2 to +3 or -2 to -3) use 'draw'/'fade' respectively. Null if spin axis not shown.",
      "avg_face_angle": float or null (average Face Angle in degrees RELATIVE TO TARGET LINE for right-handed golfer; negative=open/left, positive=closed/right. CRITICAL: true face angle is ALWAYS within ±10°, typically ±5°. DO NOT confuse with Launch Angle (10°–40°), Dynamic Loft (10°–50°), or any other loft-related column. If the column value appears to be >10° or <-10°, it is NOT the face angle — set null instead.),
      "avg_club_path": float or null (average Club Path in degrees; negative=out-to-in, positive=in-to-out),
      "avg_face_to_path": float or null (average Face-to-Path in degrees; negative=fade/slice tendency, positive=draw/hook),
      "avg_spin_axis_deg": float or null (average Spin Axis in degrees; positive=draw tilt, negative=fade tilt),
      "avg_spin_rate_rpm": integer or null (average Spin Rate in rpm),
      "avg_smash_factor": float or null (average Smash Factor; typically 1.20-1.50),
      "avg_club_speed_mph": float or null (average Club Speed in mph),
      "avg_ball_speed_mph": float or null (average Ball Speed in mph),
      "avg_attack_angle_deg": float or null (average Attack Angle in degrees; negative=downward, positive=upward),
      "avg_launch_angle_deg": float or null (average Launch Angle in degrees),
      "source_images": []
    }
  ]
}

Rules:
- Create one entry per club. If multiple clubs appear, create multiple entries.
- If individual shot distances are shown in a table, compute avg/max/min from those rows.
- For L/R direction fields (Carry Side, Launch Dir etc.): treat L as negative, R as positive for right-handed golfer.
- DATE (important): Actively scan every part of the screenshot for a date — app title bar, session header, navigation arrows, settings panel, or any text that looks like a date/time. Common formats include "Jan 14", "14.1.2026", "Monday Jan 14", "2026-01-14". Convert to YYYY-MM-DD. Only return null if there is genuinely no date visible anywhere.
- Return ONLY the JSON object — no markdown fences, no explanation.
- Use null for any field not visible in the screenshots.
"""

REAL_ROUND_PROMPT = """
You are analysing a photo or screenshot of a golf scorecard, round summary screen,
or scoring app from a real golf course (not a simulator).

The image may be a paper scorecard photo, a mobile scoring app screenshot, or a
printed round summary. Extract ALL visible data and return a single JSON object:

{
  "rounds": [
    {
      "course": "course name as shown",
      "date": "YYYY-MM-DD or null",
      "score_vs_par": integer (e.g. 18 for +18, -1 for one under par, 0 for even),
      "total_strokes": integer,
      "par": integer (total course par),
      "holes_played": integer (18 or 9),
      "fairways_hit_pct": integer or null,
      "avg_drive_m": integer or null,
      "longest_drive_m": integer or null,
      "gir_pct": integer or null,
      "scrambling_pct": integer or null,
      "total_putts": integer or null,
      "putts_per_hole": float or null,
      "holes": [
        {
          "number": integer (hole number 1-18),
          "par": integer (3, 4, or 5 — from the Par row),
          "strokes": integer (player result — from the Result/Score row),
          "stroke_index": integer or null (from the HCP/Index row — difficulty ranking 1-18),
          "distance_m": integer or null (hole length in metres, if shown on scorecard),
          "gir": true or false or null (reached green in regulation — infer as true if strokes - putts <= par - 2 when putts is known),
          "putts": integer or null (putts on this hole — extract from Putts row if visible),
          "fairway_hit": true or false or null (par 4/5 only — extract from FW row if visible, null for par 3)
        }
      ],
      "source_images": [],
      "source_type": "real"
    }
  ]
}

Rules:
- ALWAYS extract hole-by-hole data when a scorecard table is visible.
- The Hole row = hole number; Par row = par; Result/Score row = strokes played.
- The HCP row (if shown) = stroke_index (difficulty ranking, not the player's handicap).
- If a Putts row is visible, extract putts per hole.
- If a Fairways row is visible, extract fairway_hit per hole (true/false, par 4/5 only).
- Infer GIR = true when strokes - putts <= par - 2 (i.e. player reached green in regulation).
- Calculate score_vs_par = total_strokes - par.
- If only 9 holes are visible, extract those 9 and set holes_played = 9.
- Return ONLY the JSON object — no markdown fences, no explanation.
- Use null for any field not visible in the image.
"""

PROMPTS = {
    "round":      ANALYSIS_PROMPT,
    "real_round": REAL_ROUND_PROMPT,
    "range":      RANGE_PROMPT,
}

COACH_SYSTEM_PROMPT = """You are an expert personal golf coach for Joonas. \
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
- Do not repeat the question back to the player"""


# ── Duplicate helpers ────────────────────────────────────────────────────────
def _is_duplicate_round(store, r):
    """True if a round with the same course + date + total_strokes already exists."""
    course  = (r.get("course") or "").strip().lower()
    date    = r.get("date")
    strokes = r.get("total_strokes")
    if not course or not date or strokes is None:
        return False   # can't reliably detect duplicates without all three fields
    for existing in store.get("rounds", []):
        if (
            (existing.get("course") or "").strip().lower() == course
            and existing.get("date") == date
            and existing.get("total_strokes") == strokes
        ):
            return True
    return False


def _is_duplicate_session(store, s):
    """True if an identical range session already exists.

    Key = club + date + shots + avg_carry_m.
    Including shot count prevents false positives when the same club is
    practised twice on the same day (same avg carry is plausible, but
    hitting exactly the same number of balls is unlikely).
    """
    club  = (s.get("club") or "").strip().lower()
    date  = s.get("date")
    carry = s.get("avg_carry_m")
    shots = s.get("shots")          # None is allowed — treated as wildcard below
    if not club or not date or carry is None:
        return False
    for existing in store.get("range_sessions", []):
        if (
            (existing.get("club") or "").strip().lower() == club
            and existing.get("date") == date
            and existing.get("avg_carry_m") == carry
            # Only compare shots when both sides have a value
            and (shots is None or existing.get("shots") is None
                 or existing.get("shots") == shots)
        ):
            return True
    return False


# ── HTTP Handler ────────────────────────────────────────────────────────────
class GolfHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Only log errors and POST requests to keep console clean
        if args and str(args[1]) not in ("200",):
            print(f"  [{args[1]}] {self.path}")

    def _send(self, data, status: int = 200, content_type: str = "application/json"):
        if isinstance(data, dict):
            body = json.dumps(data, ensure_ascii=False).encode()
        else:
            body = data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if HTML_FILE.exists():
                self._send(HTML_FILE.read_bytes(), content_type="text/html; charset=utf-8")
            else:
                self._send({"error": "golf_dashboard.html not found in same folder"}, 404)

        elif self.path == "/api/rounds":
            if DATA_FILE.exists():
                data = json.loads(DATA_FILE.read_text())
            else:
                data = {"rounds": [], "range_sessions": [], "processed_images": []}
            self._send(data)

        else:
            self._send({"error": "Not found"}, 404)

    def do_POST(self):
        try:
            length  = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send({"error": f"Bad request: {e}"}, 400)
            return

        if self.path == "/api/analyse":
            self._handle_analyse(payload)
        elif self.path == "/api/save":
            self._handle_save(payload)
        elif self.path == "/api/coach":
            self._handle_coach(payload)
        elif self.path == "/api/delete":
            self._handle_delete(payload)
        else:
            self._send({"error": "Not found"}, 404)

    # ── Analyse images via Anthropic ──────────────────────────────────────
    def _handle_analyse(self, payload: dict):
        api_key = payload.get("api_key", "").strip()
        images  = payload.get("images", [])   # [{name, data, media_type}, ...]

        if not api_key:
            self._send({"error": "API key is required"}, 400); return
        if not images:
            self._send({"error": "No images provided"}, 400); return

        mode   = payload.get("mode", "round")   # "round" | "real_round" | "range"

        # Mode-specific limits:
        #   range  — many small screenshots, large JSON output (12+ fields/session)
        #   round  — fewer images but rich per-hole data; shots[] array added per
        #            hole pushes output well past 4096 (18 holes × ~3 shots × 5
        #            fields ≈ 270 extra values on top of hole-level data)
        if mode == "range":
            img_cap    = 60    # range screenshots are lighter; allow more
            max_tokens = 8192  # many sessions × many fields = large JSON
        else:
            img_cap    = 40    # scorecard images can be large/complex
            max_tokens = 8192  # bumped from 4096: shots[] per hole ~doubles output

        # Build message content
        content = []
        for img in images[:img_cap]:
            content.append({
                "type":   "image",
                "source": {
                    "type":       "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data":       img["data"],
                },
            })
            # Add filename and file date as context hints for Claude
            parts = []
            if img.get("name"):         parts.append(f"Filename: {img['name']}")
            if img.get("lastModified"): parts.append(f"File date: {img['lastModified']}")
            if parts:
                content.append({"type": "text", "text": "[" + " | ".join(parts) + "]"})
        prompt = PROMPTS.get(mode, ANALYSIS_PROMPT)
        content.append({"type": "text", "text": prompt})

        body = json.dumps({
            "model":      CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages":   [{"role": "user", "content": content}],
        }).encode()

        req = urllib.request.Request(
            API_URL, data=body,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            method="POST",
        )

        sent = min(len(images), img_cap)
        if len(images) > img_cap:
            print(f"  ℹ {len(images)} image(s) received — capped at {img_cap} for mode={mode}")
        print(f"  → Sending {sent} image(s) to Claude ({CLAUDE_MODEL}, max_tokens={max_tokens})…")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            text = result["content"][0]["text"].strip()
            stop_reason = result.get("stop_reason", "")
            if stop_reason == "max_tokens":
                print(f"  ⚠ Response hit max_tokens ({max_tokens}) — JSON may be truncated. "
                      f"Try sending fewer images or splitting into batches.")

            # Extract the JSON block (Claude may wrap in fences even when asked not to)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("No JSON object found in Claude response")
            extracted = json.loads(text[start:end])

            rounds  = extracted.get("rounds", [])
            range_s = extracted.get("range_sessions", [])
            print(f"  ✓ Extracted {len(rounds)} round(s), {len(range_s)} range session(s)  [mode={mode}]")

            # Apply file-date fallback: if Claude couldn't find a date in the screenshot,
            # use the file's lastModified date (sent by the browser) as a fallback.
            # This is better than leaving date=null, which forces manual entry every time.
            if mode == "range" and range_s:
                file_dates = [img.get("lastModified") for img in images[:img_cap]
                              if img.get("lastModified")]
                if file_dates:
                    fallback = file_dates[0]
                    for s in range_s:
                        if not s.get("date"):
                            s["date"] = fallback
                            print(f"  📅 Date fallback applied for {s.get('club','?')} → {fallback} (file date)")

            self._send({"success": True, "data": extracted, "mode": mode})

        except urllib.error.HTTPError as e:
            err = e.read().decode()
            print(f"  ✗ API error {e.code}: {err[:200]}")
            self._send({"error": f"Anthropic API error {e.code}: {err}"}, 500)
        except Exception as e:
            print(f"  ✗ Error: {e}")
            self._send({"error": str(e)}, 500)

    # ── Save to data store ────────────────────────────────────────────────
    def _handle_save(self, payload: dict):
        target     = payload.get("target", "rounds")   # "rounds" | "range_sessions"
        new_rounds = payload.get("rounds", [])
        new_range  = payload.get("range_sessions", [])

        items = new_range if target == "range_sessions" else new_rounds
        if not items:
            self._send({"error": f"No {target} provided"}, 400); return

        if DATA_FILE.exists():
            store = json.loads(DATA_FILE.read_text())
        else:
            store = {"rounds": [], "range_sessions": [], "processed_images": []}

        store.setdefault("rounds", [])
        store.setdefault("range_sessions", [])

        if target == "range_sessions":
            saved, skipped = 0, 0
            for s in items:
                if _is_duplicate_session(store, s):
                    skipped += 1
                    print(f"  ⚠ Skipped duplicate session: {s.get('club')} {s.get('date')}")
                else:
                    s.setdefault("source_images", [])
                    store["range_sessions"].append(s)
                    saved += 1
            total = len(store["range_sessions"])
            print(f"  ✓ Saved {saved} range session(s), skipped {skipped} duplicate(s) — total: {total}")
            self._send({"success": True, "saved": saved, "skipped": skipped, "total_range_sessions": total})
        else:
            saved, skipped = 0, 0
            for r in items:
                if _is_duplicate_round(store, r):
                    skipped += 1
                    print(f"  ⚠ Skipped duplicate round: {r.get('course')} {r.get('date')} ({r.get('total_strokes')} strokes)")
                else:
                    r.setdefault("holes", [])
                    r.setdefault("source_images", [])
                    store["rounds"].append(r)
                    saved += 1
            total = len(store["rounds"])
            print(f"  ✓ Saved {saved} round(s), skipped {skipped} duplicate(s) — total: {total}")
            self._send({"success": True, "saved": saved, "skipped": skipped, "total_rounds": total})

        DATA_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False))

    # ── Delete a round or range session ───────────────────────────────────
    def _handle_delete(self, payload: dict):
        item_type = payload.get("type")          # "round" or "range"
        index     = payload.get("index")         # index in full array

        if item_type not in ("round", "range") or not isinstance(index, int):
            self._send({"error": "Invalid request: need type (round|range) and integer index"}, 400)
            return

        if not DATA_FILE.exists():
            self._send({"error": "No data file found"}, 404)
            return

        store = json.loads(DATA_FILE.read_text())
        key   = "rounds" if item_type == "round" else "range_sessions"
        items = store.get(key, [])

        if index < 0 or index >= len(items):
            self._send({"error": f"Index {index} out of range (0–{len(items)-1})"}, 400)
            return

        deleted = items.pop(index)
        store[key] = items
        DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2))

        label = deleted.get("course") or deleted.get("club", "?")
        date  = deleted.get("date", "")
        print(f"  🗑 Deleted {item_type}: {label} {date}")
        self._send({"ok": True, "deleted": label})

    # ── AI Coach chat ─────────────────────────────────────────────────────
    def _handle_coach(self, payload: dict):
        api_key  = payload.get("api_key", "").strip()
        messages = payload.get("messages", [])
        context  = payload.get("context", {})

        if not api_key:
            self._send({"error": "API key required"}, 400); return
        if not messages:
            self._send({"error": "No messages provided"}, 400); return

        # Cap history to last 20 messages to control token usage
        messages = list(messages[-20:])

        # On the very first user turn, prepend the data context to the message content
        ctx_text = context.get("text", "")
        ctx_type = context.get("type", "dashboard")
        if ctx_text and len(messages) == 1 and messages[0].get("role") == "user":
            messages[0] = {
                "role": "user",
                "content": f"[MY GOLF DATA]\n{ctx_text}\n\n[MY QUESTION]\n{messages[0]['content']}",
            }

        body = json.dumps({
            "model":      CLAUDE_MODEL,
            "max_tokens": 1024,
            "system":     COACH_SYSTEM_PROMPT,
            "messages":   messages,
        }).encode()

        req = urllib.request.Request(
            API_URL, data=body,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            method="POST",
        )

        print(f"  → Coach request: {ctx_type} context, {len(messages)} msg(s)")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
            reply = result["content"][0]["text"].strip()
            print(f"  ✓ Coach replied ({len(reply)} chars)")
            self._send({"success": True, "reply": reply})
        except urllib.error.HTTPError as e:
            err = e.read().decode()
            print(f"  ✗ Coach API error {e.code}: {err[:200]}")
            self._send({"error": f"API error {e.code}: {err}"}, 500)
        except Exception as e:
            print(f"  ✗ Coach error: {e}")
            self._send({"error": str(e)}, 500)


# ── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print("  ⛳  Golf Analytics Dashboard")
    print(f"  ──────────────────────────────────────")
    print(f"  Server:    http://localhost:{PORT}")
    print(f"  Data file: {DATA_FILE.name}")
    print(f"  Dashboard: {HTML_FILE.name}")
    print(f"  ──────────────────────────────────────")
    print(f"  Open http://localhost:{PORT} in your browser")
    print(f"  Press Ctrl+C to stop")
    print()

    if not HTML_FILE.exists():
        print(f"  ⚠  Warning: {HTML_FILE.name} not found — make sure it's in the same folder")

    server = HTTPServer(("", PORT), GolfHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
