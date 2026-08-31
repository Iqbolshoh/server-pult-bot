#!/usr/bin/env bash
# Server Pult installer. Idempotent: run it again to repair or reconfigure.
#
#   ./install.sh                 interactive
#   BOT_TOKEN=... ADMIN_CHAT_ID=... PULT_LANG=uz ./install.sh --yes
#
# It never overwrites an answer you already gave: existing values in .env and
# config.json are kept unless you type a new one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${SERVER_PULT_HOME:-$HERE}"
SERVICE="server-pult-bot"
ASSUME_YES=0
case "${1:-}" in --yes|-y) ASSUME_YES=1 ;; esac

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

ask() {  # ask <prompt> <current value>
    local prompt="$1" current="${2:-}" answer=""
    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        printf '%s' "$current"; return
    fi
    if [ -n "$current" ]; then
        read -r -p "$prompt [$current]: " answer </dev/tty || true
    else
        read -r -p "$prompt: " answer </dev/tty || true
    fi
    printf '%s' "${answer:-$current}"
}

say "Server Pult -- install"
say "----------------------"

# ---------------------------------------------------------------- requirements
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 is not installed"
"$PYTHON" - <<'PY' || die "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
ok "python3 $("$PYTHON" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

# The engines are optional one by one: the bot runs with whichever it finds, and
# /doctor names the missing one instead of the install failing here.
ENGINES_FOUND=0
for engine in claude agy; do
    if command -v "$engine" >/dev/null 2>&1; then
        ok "$engine -> $(command -v "$engine")"
        ENGINES_FOUND=$((ENGINES_FOUND + 1))
    else
        warn "$engine is not on PATH -- that engine will be skipped (see /doctor)"
    fi
done
[ "$ENGINES_FOUND" -gt 0 ] || warn "no engine found; install claude or agy before running a job"

# ---------------------------------------------------------------- .env
ENV_FILE="$HOME_DIR/.env"
CUR_TOKEN=""; CUR_ADMIN=""; CUR_FLAGS=""
if [ -f "$ENV_FILE" ]; then
    CUR_TOKEN="$(sed -n 's/^BOT_TOKEN=//p' "$ENV_FILE" | head -1)"
    CUR_ADMIN="$(sed -n 's/^ADMIN_CHAT_ID=//p' "$ENV_FILE" | head -1)"
    CUR_FLAGS="$(sed -n 's/^AGY_FLAGS=//p' "$ENV_FILE" | head -1)"
fi
TOKEN="$(ask 'Telegram bot token (from @BotFather)' "${BOT_TOKEN:-$CUR_TOKEN}")"
[ -n "$TOKEN" ] || die "a bot token is required; get one from @BotFather"
ADMIN="$(ask 'Your Telegram user id (blank = pair with a code later)' "${ADMIN_CHAT_ID:-$CUR_ADMIN}")"
# The offer is whatever locales/ actually ships -- a hardcoded list here is how
# Tajik came to be rejected by the installer on the day it was added.
LANGS="$(cd "$HERE/locales" 2>/dev/null && ls *.json 2>/dev/null | sed 's/\.json$//' | tr '\n' ' ')"
LANGS="${LANGS:-uz ru en}"
LANG_CODE="$(ask "Language ($(echo $LANGS | sed 's/ / \/ /g'))" "${PULT_LANG:-$( [ -f "$HOME_DIR/config.json" ] && "$PYTHON" -c "import json;print(json.load(open('$HOME_DIR/config.json')).get('language','uz'))" || echo uz)}")"
case " $LANGS " in *" $LANG_CODE "*) ;; *) warn "unknown language '$LANG_CODE', using uz"; LANG_CODE=uz ;; esac

umask 077
{
    echo "# Secrets. Never commit this file."
    echo "BOT_TOKEN=$TOKEN"
    echo "ADMIN_CHAT_ID=$ADMIN"
    echo "LOCAL_API_PORT=${LOCAL_API_PORT:-7799}"
    echo ""
    echo "# Permission flags passed to every agy run. Decide this yourself."
    echo "# Example: AGY_FLAGS=--mode accept-edits"
    echo "AGY_FLAGS=$CUR_FLAGS"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok ".env written"

# ---------------------------------------------------------------- config.json
SERVER_PULT_HOME="$HOME_DIR" "$PYTHON" - "$LANG_CODE" "$HERE" <<'PY' >/dev/null
import sys
sys.path.insert(0, sys.argv[2])
from pult.config import CFG, save_config, ensure_pairing_code
CFG["language"] = sys.argv[1]
save_config()
ensure_pairing_code()
PY
ok "config.json ready"

# ---------------------------------------------------------------- service unit
install_supervisor() {
    local conf="/etc/supervisor/conf.d/$SERVICE.conf"
    cat > "$conf" <<CONF
; Server Pult -- one Telegram bot driving two AI coding engines (claude + agy).
; HOME must point at the account the engines are logged in as.
[program:$SERVICE]
command=$PYTHON $HERE/bot.py
directory=$HERE
autostart=true
autorestart=true
startsecs=5
startretries=20
user=$(id -un)
environment=HOME="$HOME",PATH="$PATH",PYTHONUNBUFFERED="1",SERVER_PULT_HOME="$HOME_DIR"
stopsignal=TERM
stopwaitsecs=20
killasgroup=true
stopasgroup=true
redirect_stderr=true
stdout_logfile=/var/log/$SERVICE.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
CONF
    supervisorctl reread >/dev/null && supervisorctl update >/dev/null
    supervisorctl restart "$SERVICE" >/dev/null 2>&1 || supervisorctl start "$SERVICE" >/dev/null
    ok "supervisor program $SERVICE installed and started"
    say "  logs: tail -f /var/log/$SERVICE.log"
}

install_systemd() {
    local unit="/etc/systemd/system/$SERVICE.service"
    cat > "$unit" <<UNIT
[Unit]
Description=Server Pult -- Telegram bot for two AI coding engines
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$HERE
Environment=HOME=$HOME
Environment=PYTHONUNBUFFERED=1
Environment=SERVER_PULT_HOME=$HOME_DIR
ExecStart=$PYTHON $HERE/bot.py
Restart=always
RestartSec=5
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now "$SERVICE" >/dev/null
    systemctl restart "$SERVICE"
    ok "systemd unit $SERVICE installed and started"
    say "  logs: journalctl -u $SERVICE -f"
}

if [ "$(id -u)" != "0" ]; then
    warn "not running as root -- skipping service installation"
    say  "  start it by hand: SERVER_PULT_HOME=$HOME_DIR $PYTHON $HERE/bot.py"
elif command -v supervisorctl >/dev/null 2>&1 && [ -d /etc/supervisor/conf.d ]; then
    install_supervisor
elif command -v systemctl >/dev/null 2>&1; then
    install_systemd
else
    warn "neither supervisor nor systemd found -- start it yourself:"
    say  "  SERVER_PULT_HOME=$HOME_DIR $PYTHON $HERE/bot.py"
fi

# ---------------------------------------------------------------- what next
BOT_NAME="$(curl -s -m 10 "https://api.telegram.org/bot$TOKEN/getMe" \
    | sed -n 's/.*"username":"\([^"]*\)".*/\1/p' || true)"
PAIRING="$(SERVER_PULT_HOME="$HOME_DIR" "$PYTHON" -c "
import json
try: print(json.load(open('$HOME_DIR/config.json')).get('pairing_code',''))
except Exception: print('')")"

say ""
say "Done."
[ -n "$BOT_NAME" ] && say "  Open: https://t.me/$BOT_NAME"
if [ -n "$PAIRING" ]; then
    say "  Send this code to the bot to claim it: $PAIRING"
else
    say "  Already paired -- send /start to the bot."
fi
say "  If anything is wrong, send /doctor."
