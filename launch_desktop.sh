#!/usr/bin/env bash
REAL_SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${REAL_SCRIPT}")" && pwd)"

# 1. Ensure backend service is running on port 8001
if ! curl -s http://127.0.0.1:8001/health > /dev/null 2>&1; then
    echo "Starting OmniAgent backend engine..."
    nohup "${SCRIPT_DIR}/.venv/bin/uvicorn" server:app --app-dir "${SCRIPT_DIR}" --host 127.0.0.1 --port 8001 > /dev/null 2>&1 &
    
    # Wait up to 5s for backend startup
    for i in {1..10}; do
        if curl -s http://127.0.0.1:8001/health > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
fi

URL="http://127.0.0.1:8001"

# 2. Try launching as an isolated native app window (no address bar, no browser tabs)
if command -v google-chrome >/dev/null 2>&1; then
    exec google-chrome --app="$URL" --class="OmniAgent" --name="OmniAgent" --user-data-dir="/tmp/omniagent_chrome_profile"
elif command -v chromium >/dev/null 2>&1; then
    exec chromium --app="$URL" --class="OmniAgent" --name="OmniAgent" --user-data-dir="/tmp/omniagent_chrome_profile"
elif command -v brave-browser >/dev/null 2>&1; then
    exec brave-browser --app="$URL" --class="OmniAgent" --name="OmniAgent"
elif command -v msedge >/dev/null 2>&1; then
    exec msedge --app="$URL" --class="OmniAgent" --name="OmniAgent"
elif command -v firefox >/dev/null 2>&1; then
    # Firefox isolated standalone window
    exec firefox --new-window "$URL" --class "OmniAgent" --name "OmniAgent"
else
    # Fallback default handler
    xdg-open "$URL"
fi
