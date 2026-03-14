#!/usr/bin/env python3
"""
Golf Dashboard Server v1.0
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
      "holes": [],
      "source_images": []
    }
  ],
  "range_sessions": []
}

Rules:
- Group hole-map images with their corresponding round summary into ONE round entry.
- If you see two different courses, create two separate round entries.
- Infer par = total_strokes - score_vs_par.
- Return ONLY the JSON object — no explanation, no markdown fences.
- If a field is genuinely not visible, use null.
"""

RANGE_PROMPT = """
You are analysing screenshots from the Virtual Golf 3 golf simulator range / practice mode,
or photos from a real driving range session.

The images may show shot-by-shot distance data, accuracy stats, club info, or dispersion maps.

Extract all club data visible and return a single JSON object:

{
  "range_sessions": [
    {
      "date": "YYYY-MM-DD or null",
      "club": "exact club name as shown, e.g. Driver, 3W, 5I, 7I, 9I, PW, 52°, 56°, 60°",
      "shots": integer or null,
      "avg_carry_m": integer or null,
      "avg_total_m": integer or null,
      "max_carry_m": integer or null,
      "min_carry_m": integer or null,
      "dispersion_m": integer or null (lateral spread in metres — smaller = tighter),
      "target_hit_pct": integer or null (% of shots on target),
      "avg_from_pin_m": float or null (average distance from pin/target),
      "source_images": []
    }
  ]
}

Rules:
- Create one entry per club. If multiple clubs appear, create multiple entries.
- If individual shot distances are shown, derive avg/max/min from them.
- Return ONLY the JSON object — no markdown fences, no explanation.
- Use null for any field not visible.
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
          "stroke_index": integer or null (from the HCP/Index row — difficulty ranking 1-18)
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

        # Build message content
        content = []
        for img in images[:40]:   # cap at 40 to stay within token limits
            content.append({
                "type":   "image",
                "source": {
                    "type":       "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data":       img["data"],
                },
            })
            if img.get("name"):
                content.append({"type": "text", "text": f"[Filename: {img['name']}]"})
        mode   = payload.get("mode", "round")   # "round" | "real_round" | "range"
        prompt = PROMPTS.get(mode, ANALYSIS_PROMPT)
        content.append({"type": "text", "text": prompt})

        body = json.dumps({
            "model":      CLAUDE_MODEL,
            "max_tokens": 4096,
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

        print(f"  → Sending {len(images)} image(s) to Claude ({CLAUDE_MODEL})…")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            text = result["content"][0]["text"].strip()

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
            for s in items:
                s.setdefault("source_images", [])
                store["range_sessions"].append(s)
            total = len(store["range_sessions"])
            print(f"  ✓ Saved {len(items)} range session(s) — total now: {total}")
            self._send({"success": True, "total_range_sessions": total})
        else:
            for r in items:
                r.setdefault("holes", [])
                r.setdefault("source_images", [])
                store["rounds"].append(r)
            total = len(store["rounds"])
            print(f"  ✓ Saved {len(items)} round(s) — total now: {total}")
            self._send({"success": True, "total_rounds": total})

        DATA_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False))


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
