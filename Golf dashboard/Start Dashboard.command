#!/bin/bash
# Golf Analytics Dashboard — double-click this file in Finder to start

cd "$(dirname "$0")"

# Kill any stray server already on port 8080
lsof -ti:8080 | xargs kill -9 2>/dev/null || true
sleep 0.3

echo ""
echo "⛳  Starting Golf Analytics Dashboard…"
echo ""

# Start the server in the background
python3 golf_server.py &
SERVER_PID=$!

# Give it a moment to bind the port
sleep 1

# Open the dashboard in the default browser
open http://localhost:8080

echo "   Dashboard open at http://localhost:8080"
echo "   Close this window (or press Ctrl+C) to stop the server."
echo ""

# Keep the Terminal window open so the server keeps running
wait $SERVER_PID
