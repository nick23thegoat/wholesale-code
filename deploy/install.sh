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
# `-` not `:-`: an explicitly empty REPO_DIR must fail the check below
# rather than quietly becoming the default.
REPO_DIR="${REPO_DIR-$INSTALL_DIR/wholesale-code}"
VENV_DIR="${VENV_DIR:-$INSTALL_DIR/venv}"
DATA_DIR="${DATA_DIR:-/var/lib/wholesale}"
ENV_FILE="${ENV_FILE:-/etc/wholesale/env}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

# --- REPO_DIR sanity ------------------------------------------------------
# Everything below runs `chown -R` and `chmod -R` against this path. A wrong
# value is not a failed install, it is a damaged system, so this fails closed
# on anything that is not recognisably a checkout of this project.
die() { echo "REFUSING TO RUN: $*" >&2; exit 1; }

[ -n "${REPO_DIR:-}" ] || die "REPO_DIR is empty."
case "$REPO_DIR" in
  /) die "REPO_DIR is '/'. A recursive chown against the filesystem root would break this machine." ;;
  */..*) die "REPO_DIR contains '..': $REPO_DIR" ;;
  /*) : ;;
  *) die "REPO_DIR must be an absolute path, got '$REPO_DIR'." ;;
esac

# Resolve symlinks before comparing, so a link cannot point the recursive
# operations somewhere other than where the checks looked.
REPO_DIR="$(readlink -f -- "$REPO_DIR" 2>/dev/null || echo "$REPO_DIR")"
[ "$REPO_DIR" != "/" ] || die "REPO_DIR resolves to '/'."

for shallow in /etc /usr /var /home /root /opt /srv /boot /bin /sbin /lib /tmp; do
  [ "$REPO_DIR" = "$shallow" ] && die "REPO_DIR is a system directory: $REPO_DIR"
done

[ -d "$REPO_DIR" ] || die "no checkout at $REPO_DIR — clone it there first."

# It must actually look like this project, not merely exist.
for required in wholesale_engine/__init__.py wholesale_engine/main.py deploy/install.sh; do
  [ -f "$REPO_DIR/$required" ] || die "$REPO_DIR is not a checkout of this project (missing $required)."
done

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

say "Credential files"
# `chmod -R go-w` above removes WRITE bits and leaves READ alone, so a .env
# created with the usual umask stays 0644 — world-readable, holding API keys.
# Named explicitly rather than by a blanket chmod across the tree: making
# every file 0600 would also make the code unreadable to the service user.
# Contents are never touched.
credentials_found=0
for candidate in "$REPO_DIR/.env" "$REPO_DIR/.envrc"; do
  if [ -f "$candidate" ]; then
    chown root:root -- "$candidate"
    chmod 0600 -- "$candidate"
    echo "    $candidate -> 0600 root:root"
    credentials_found=1
  fi
done
[ "$credentials_found" -eq 1 ] || echo "    none in the checkout (credentials belong in $ENV_FILE)"

say "systemd units"
# A unit you edited on the box is a decision someone made deliberately, so it
# is copied aside before being replaced rather than silently overwritten. The
# repo version still wins — that keeps a re-run predictable — but the edit is
# recoverable and the script says where it went.
stamp="$(date +%Y%m%d-%H%M%S)"
for unit in wholesale-web.service wholesale-backup.service wholesale-backup.timer \
            wholesale-hunt.service wholesale-hunt.timer; do
  target="/etc/systemd/system/$unit"
  if [ -f "$target" ] && ! cmp -s "$REPO_DIR/deploy/$unit" "$target"; then
    cp -p -- "$target" "$target.bak-$stamp"
    echo "    $unit differs from the repo — saved yours to $target.bak-$stamp"
  fi
  install -m 0644 "$REPO_DIR/deploy/$unit" "$target"
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
