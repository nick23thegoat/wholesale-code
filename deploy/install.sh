#!/usr/bin/env bash
# Prepare a VPS to run the wholesale engine. Idempotent: safe to re-run.
#
#     sudo bash deploy/install.sh
#
# What it does NOT do, on purpose:
#   * it does not start anything listening on a public interface
#   * it does not configure Tailscale for you — that needs your account
#   * it does not write credentials; you fill /etc/wholesale/env in by hand
#
# Read deploy/README.md before running this. The section on why the dashboard
# binds loopback is the one that matters.

set -euo pipefail

APP_USER="${APP_USER:-wholesale}"
INSTALL_DIR="${INSTALL_DIR:-/opt/wholesale}"
REPO_DIR="${REPO_DIR:-$INSTALL_DIR/wholesale-code}"
VENV_DIR="${VENV_DIR:-$INSTALL_DIR/venv}"
DATA_DIR="${DATA_DIR:-/var/lib/wholesale}"
ENV_FILE="${ENV_FILE:-/etc/wholesale/env}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
[ -d "$REPO_DIR" ] || { echo "no checkout at $REPO_DIR — clone it there first" >&2; exit 1; }

say "Service user: $APP_USER"
if id "$APP_USER" >/dev/null 2>&1; then
  echo "    already exists"
else
  # No login shell and no home: this account exists to own files and run a
  # read-only web process, and nothing else.
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "    created"
fi

say "Directories"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR/backups"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR/cache"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR/config"
# 0750 rather than 0755: these hold owner names and mailing addresses, and
# other accounts on the box have no business reading them.
echo "    $DATA_DIR (0750, owned by $APP_USER)"

say "Virtual environment"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
  echo "    created at $VENV_DIR"
else
  echo "    already at $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/wholesale_engine/requirements.txt"
echo "    dependencies installed: $("$VENV_DIR/bin/python" -c 'import flask, gunicorn; print("Flask", flask.__version__ if hasattr(flask,"__version__") else "3.x", "+ gunicorn")' 2>/dev/null || echo 'see pip output')"

say "Environment file"
install -d -m 0755 "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENVEOF
# Loaded by systemd. NOT a shell script — no export, no quotes, no \$expansion.
# Keep this file 0640 and owned root:$APP_USER. Never commit it.

WHOLESALE_DATA_DIR=$DATA_DIR
WHOLESALE_MODE=TEST

# Fill these in to go live. Until then the engine runs on local CSV files.
# DATA_PROVIDER=rentcast
# RENTCAST_API_KEY=
# MAX_RENTCAST=50
ENVEOF
  echo "    created $ENV_FILE — fill in your credentials"
else
  echo "    already exists, left alone"
fi
chown "root:$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

say "Code ownership"
# The checkout stays root-owned and the service user only reads it, so a
# compromised web process cannot rewrite the code it is running.
chown -R root:root "$REPO_DIR"
chmod -R go-w "$REPO_DIR"
echo "    $REPO_DIR is root-owned, not writable by $APP_USER"

say "systemd units"
for unit in wholesale-web.service wholesale-backup.service wholesale-backup.timer \
            wholesale-hunt.service wholesale-hunt.timer; do
  install -m 0644 "$REPO_DIR/deploy/$unit" "/etc/systemd/system/$unit"
  echo "    installed $unit"
done
systemctl daemon-reload

say "Resolved paths (check the database is where you expect)"
sudo -u "$APP_USER" env "WHOLESALE_DATA_DIR=$DATA_DIR" \
  "$VENV_DIR/bin/python" -c \
  'import sys; sys.path.insert(0, "'"$REPO_DIR"'"); from wholesale_engine import paths; print(paths.describe())'

cat <<'NEXT'

==> Not done yet. Three things are yours:

  1. Fill in /etc/wholesale/env, then:
         sudo systemctl enable --now wholesale-web
         sudo systemctl enable --now wholesale-backup.timer

  2. Tailscale, if it is not already up:
         curl -fsSL https://tailscale.com/install.sh | sh
         sudo tailscale up
         sudo tailscale serve --bg 8000

  3. PROVE the public interface is closed. Do not skip this:
         deploy/README.md -> "Proving the network path"

NEXT
