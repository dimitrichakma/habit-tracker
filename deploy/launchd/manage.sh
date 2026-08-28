#!/usr/bin/env bash
# Manage the habit-tracker launchd agents (macOS): backend + Telegram bot.
#
#   ./deploy/launchd/manage.sh install     # render templates, load, start
#   ./deploy/launchd/manage.sh uninstall   # stop and remove both agents
#   ./deploy/launchd/manage.sh start       # kickstart both
#   ./deploy/launchd/manage.sh stop        # stop both (stay installed)
#   ./deploy/launchd/manage.sh restart     # kickstart -k both
#   ./deploy/launchd/manage.sh status      # launchd state for both
#   ./deploy/launchd/manage.sh logs        # tail -f the four log files
#
# `stop` is what to run before `./run.sh` / a --reload dev server so nothing
# fights over port 8000; `start` again when you're done.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(com.dimitri.habit-tracker.backend com.dimitri.habit-tracker.bot)

uv_path() {
    command -v uv || { echo "uv not found on PATH" >&2; exit 1; }
}

cmd_install() {
    local uv; uv="$(uv_path)"
    mkdir -p "$LAUNCH_AGENTS" "$PROJECT_DIR/logs"
    for label in "${LABELS[@]}"; do
        local dest="$LAUNCH_AGENTS/$label.plist"
        sed -e "s|@@UV@@|$uv|g" -e "s|@@PROJECT_DIR@@|$PROJECT_DIR|g" \
            "$SCRIPT_DIR/$label.plist" > "$dest"
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$dest"
        echo "installed $label"
    done
    echo "Done. Backend on http://127.0.0.1:8000 ; bot polling Telegram."
}

cmd_uninstall() {
    for label in "${LABELS[@]}"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        rm -f "$LAUNCH_AGENTS/$label.plist"
        echo "removed $label"
    done
}

cmd_start() {
    for label in "${LABELS[@]}"; do launchctl kickstart "$DOMAIN/$label"; done
}

cmd_stop() {
    # bootout stops the job; RunAtLoad re-runs it on next `bootstrap`/login,
    # so pair this with `start` (kickstart) rather than expecting it to stay
    # down across a reboot.
    for label in "${LABELS[@]}"; do launchctl bootout "$DOMAIN/$label" 2>/dev/null || true; done
    echo "stopped (still installed; run 'start' or 'install' to bring back)"
}

cmd_restart() {
    for label in "${LABELS[@]}"; do launchctl kickstart -k "$DOMAIN/$label"; done
}

cmd_status() {
    for label in "${LABELS[@]}"; do
        echo "== $label =="
        launchctl print "$DOMAIN/$label" 2>/dev/null | grep -E '^\s*(state|pid|last exit code) =' || echo "  not loaded"
    done
}

cmd_logs() {
    exec tail -n 20 -F \
        "$PROJECT_DIR/logs/backend.out.log" "$PROJECT_DIR/logs/backend.err.log" \
        "$PROJECT_DIR/logs/bot.out.log" "$PROJECT_DIR/logs/bot.err.log"
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    *) echo "usage: $0 {install|uninstall|start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
