#!/bin/bash
# Install/uninstall A-ISP launchd agents
# Usage:
#   ./install.sh install    — symlink plists + load agents (replaces cron)
#   ./install.sh uninstall  — unload agents + remove symlinks
#   ./install.sh status     — show agent status

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
AGENTS=("com.aisp.morning" "com.aisp.close")

_load() {
    local label="$1"
    local plist="$LAUNCH_AGENTS_DIR/${label}.plist"
    if launchctl list "$label" &>/dev/null; then
        echo "  Unloading existing $label..."
        launchctl unload "$plist" 2>/dev/null || true
    fi
    echo "  Loading $label..."
    launchctl load "$plist"
}

_unload() {
    local label="$1"
    local plist="$LAUNCH_AGENTS_DIR/${label}.plist"
    if [ -f "$plist" ]; then
        echo "  Unloading $label..."
        launchctl unload "$plist" 2>/dev/null || true
        rm "$plist"
        echo "  Removed $plist"
    else
        echo "  $label not installed, skipping"
    fi
}

case "${1:-}" in
    install)
        echo "Installing A-ISP launchd agents..."
        mkdir -p "$LAUNCH_AGENTS_DIR"

        # Create logs dir
        mkdir -p "$SCRIPT_DIR/../../logs"

        for agent in "${AGENTS[@]}"; do
            src="$SCRIPT_DIR/${agent}.plist"
            dst="$LAUNCH_AGENTS_DIR/${agent}.plist"
            echo "  Linking $src → $dst"
            ln -sf "$src" "$dst"
            _load "$agent"
        done

        # Remove cron entries if present
        if crontab -l 2>/dev/null | grep -q "cron.sh"; then
            echo ""
            echo "  ⚠️  Found existing cron entries for A-ISP."
            echo "     Run 'crontab -e' to remove them (launchd replaces cron)."
            echo "     Or run: crontab -l | grep -v 'cron.sh' | crontab -"
        fi

        echo ""
        echo "✅ Installed. Agents will run even after sleep/wake."
        echo "   Morning: 08:00 daily"
        echo "   Close:   18:00 daily"
        echo ""
        echo "Check status: $0 status"
        echo "View logs:    tail -f logs/launchd_morning.log"
        ;;

    uninstall)
        echo "Uninstalling A-ISP launchd agents..."
        for agent in "${AGENTS[@]}"; do
            _unload "$agent"
        done
        echo "✅ Uninstalled."
        ;;

    status)
        echo "A-ISP launchd agent status:"
        for agent in "${AGENTS[@]}"; do
            if launchctl list "$agent" &>/dev/null; then
                echo "  ✅ $agent  (loaded)"
            else
                echo "  ❌ $agent  (not loaded)"
            fi
        done

        echo ""
        echo "Recent logs:"
        for log in "$SCRIPT_DIR/../../logs/launchd_"*.log; do
            if [ -f "$log" ]; then
                echo "  $(basename "$log"):"
                tail -3 "$log" 2>/dev/null | sed 's/^/    /'
            fi
        done
        ;;

    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac
