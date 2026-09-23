#!/bin/bash
# Double-click to start HYL's Studio. It runs only on this Mac.
cd "$(dirname "$0")/app" || exit 1
PORT=8765
if lsof -ti :$PORT >/dev/null 2>&1; then
  echo "HYL's Studio is already running. Opening it..."; open "http://localhost:$PORT"; exit 0
fi
(sleep 2; open "http://localhost:$PORT") &
exec python3 server.py
