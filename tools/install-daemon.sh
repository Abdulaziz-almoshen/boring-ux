#!/usr/bin/env bash
# Install the Boring UX local processing service as a macOS launch agent (starts at login, restarts if it dies).
#   bash tools/install-daemon.sh            # install / update
#   bash tools/install-daemon.sh --remove   # uninstall
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# NOTE: must live outside ~/Desktop, ~/Documents, ~/Downloads — macOS blocks background services from those folders.
AI="${BUX_AI_DIR:-$HOME/.boring-ux}"
PY="$AI/.venv/bin/python"
LABEL="com.boringux.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$AI/logs" "$AI/jobs" "$AI/sessions"
case "$AI" in "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*) echo "✗ $AI is inside a macOS-protected folder; use the default ~/.boring-ux (BUX_AI_DIR unset)"; exit 1;; esac

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"; echo "✓ removed $LABEL"; exit 0
fi
[ -x "$PY" ] || { echo "✗ $PY not found — run: bash tools/setup-analysis.sh first"; exit 1; }

# The repo is usually under ~/Desktop (also protected), so the service runs a STAGED COPY of tools/ from $AI/app.
# Re-run this installer after updating the repo (git pull) to refresh the copy.
APP="$AI/app"; mkdir -p "$APP"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$REPO/tools/" "$APP/tools/"
echo "  staged tools → $APP/tools"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$APP/tools/bux-daemon.py</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
    <key>BUX_AI_DIR</key><string>$AI</string>
    <key>BUX_REPO</key><string>$APP</string>
    <key>PYTORCH_ENABLE_MPS_FALLBACK</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$AI/logs/daemon.log</string>
  <key>StandardErrorPath</key><string>$AI/logs/daemon.err</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
: > "$AI/logs/daemon.err"; : > "$AI/logs/daemon.log"          # fresh logs per install — stale errors mislead
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:7331/health" >/dev/null 2>&1; then
    echo "✓ $LABEL running → $(curl -fsS http://127.0.0.1:7331/health)"; exit 0
  fi
  sleep 1
done
echo "✗ service did not answer on 127.0.0.1:7331 — see $AI/logs/daemon.err"; exit 1
