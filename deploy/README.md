# Deploying the wholesale engine to a VPS

A Linux VPS runs two things: a **weekly hunt** that spends API requests, and a
**read-only dashboard** you read from your phone. They are separate services on
purpose — the dashboard never spends money and never writes a lead.

```
iPhone → Tailscale → VPS → tailscale serve → gunicorn (127.0.0.1:8000)
                                                 → Flask → EngineService → SQLite
```

---

## The one thing to understand before you start

**The dashboard has no login.** It is private because the network makes it
private, not because the application checks who you are. Anyone who can reach
port 8000 can read every lead, every owner name and every mailing address the
provider returned.

Two independent things keep that from happening:

1. **gunicorn binds `127.0.0.1`.** The kernel refuses a connection from any
   other machine. This is not a firewall rule that can be mis-typed — there is
   no rule involved.
2. **`tailscale serve` publishes it onto your tailnet**, and only devices
   signed into your Tailscale account can reach that.

Widening the bind to `0.0.0.0` to "make it work from my phone" removes the
first of those and puts owner records on the public internet. Don't. Tailscale
is how you reach it from your phone.

---

## Prerequisites

| | |
|---|---|
| OS | Any systemd Linux (Debian/Ubuntu tested) |
| Python | 3.9+ (`python3 --version`) |
| Packages | `python3-venv`, `git`, `curl` |
| Tailscale | Installed **and logged in** — see below |
| Disk | ~200 MB, plus backups |
| RAM | 512 MB is plenty |

---

## Install

```bash
sudo mkdir -p /opt/wholesale
sudo git clone <your-repo-url> /opt/wholesale/wholesale-code
cd /opt/wholesale/wholesale-code
sudo bash deploy/install.sh
```

That creates the `wholesale` service user, `/var/lib/wholesale` (0750), a
virtualenv at `/opt/wholesale/venv`, `/etc/wholesale/env` (0640, root:wholesale),
and installs the systemd units. It starts nothing.

### Credentials

Edit `/etc/wholesale/env`. It is **read by systemd, not by a shell** — no
`export`, no quotes, no `$VAR` expansion:

```ini
WHOLESALE_DATA_DIR=/var/lib/wholesale
WHOLESALE_MODE=LIVE
DATA_PROVIDER=rentcast
RENTCAST_API_KEY=your-key-here
MAX_RENTCAST=50
```

Never commit this file. `git` already ignores `.env`; this one lives outside
the checkout entirely.

> **Why an EnvironmentFile and not the unit?** A systemd unit is world-readable
> in `/etc/systemd/system` and shows up in `systemctl cat`. The environment
> file is 0640.

### Start it

```bash
sudo systemctl enable --now wholesale-web
sudo systemctl enable --now wholesale-backup.timer
sudo systemctl enable --now wholesale-hunt.timer     # only when you are ready to spend requests
```

---

## Tailscale

Installed is not the same as configured. Check what you actually have:

```bash
tailscale status          # "Logged out" or "stopped" means NOT configured
tailscale ip -4           # your 100.x.y.z — nothing else can route to it
```

If it is not installed:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # follow the URL it prints, sign in
```

Then publish the dashboard onto the tailnet:

```bash
sudo tailscale serve --bg 8000
sudo tailscale serve status
```

`serve` (not `funnel`) is the important word. **`tailscale funnel` publishes to
the public internet** — that is precisely what must not happen here.

### From your iPhone

1. Install Tailscale from the App Store, sign into the same account.
2. Turn the VPN toggle on.
3. Open `https://<your-vps-name>.<your-tailnet>.ts.net` — `tailscale serve
   status` on the VPS prints the exact URL.
4. Share → Add to Home Screen, and it behaves like an app.

The dashboard is only reachable while the Tailscale toggle is on. That is the
security model working.

---

## Proving the network path

Do not skip this, and do not treat "Tailscale is installed" as proof.

**1. It listens on loopback only.**

```bash
sudo ss -ltnp | grep 8000
# want:  LISTEN  127.0.0.1:8000
# wrong: LISTEN  0.0.0.0:8000   <-- stop, fix the bind
```

**2. It answers on the VPS itself.**

```bash
curl -s http://127.0.0.1:8000/healthz     # {"readonly":true,"status":"ok"}
```

**3. It does NOT answer on the public address.** Run this from a machine that
is *not* on your tailnet — your phone with Tailscale **off** and wifi off is
the easiest honest test:

```bash
curl --max-time 5 http://<public-ip-of-vps>:8000/healthz
# want: a timeout or "Connection refused"
# if you get JSON back, the dashboard is on the public internet. Stop
# everything and fix the bind before doing anything else.
```

**4. It answers over Tailscale.** Phone with Tailscale on, open the `ts.net`
URL.

### Firewall, as a second layer

Loopback binding already prevents outside access. A firewall means a future
mistake is caught too:

```bash
sudo ufw allow in on tailscale0
sudo ufw allow 22/tcp
sudo ufw --force enable
sudo ufw status verbose
```

---

## Where the data lives

`WHOLESALE_DATA_DIR` moves all of it at once. With it set to `/var/lib/wholesale`:

| | |
|---|---|
| Database | `/var/lib/wholesale/leads.db` |
| Response cache | `/var/lib/wholesale/cache/` |
| API quota ledger | `/var/lib/wholesale/api_usage.json` |
| Buy box | `/var/lib/wholesale/config/buybox.json` |
| Backups | `/var/lib/wholesale/backups/` |

Check what the service actually resolved:

```bash
sudo -u wholesale env WHOLESALE_DATA_DIR=/var/lib/wholesale \
  /opt/wholesale/venv/bin/python -c \
  'import sys; sys.path.insert(0,"/opt/wholesale/wholesale-code"); \
   from wholesale_engine import paths; print(paths.describe())'
```

> **Why this matters.** Every path is absolute and resolved from the package
> location, never from the working directory — so a service with a different
> `WorkingDirectory` cannot quietly create a *second, empty* database beside
> the real one. That failure shows up as a dashboard with no leads on it and
> looks exactly like data loss. A **relative** `WHOLESALE_DATA_DIR` is ignored
> rather than honoured, for the same reason.

Permissions: `/var/lib/wholesale` is `0750 wholesale:wholesale`. The checkout at
`/opt/wholesale/wholesale-code` is root-owned and not writable by the service
user, so a compromised web process cannot rewrite its own code.

---

## Backups

Nightly at 03:00 (+ up to 30 min jitter), `Persistent=true` so a VPS that was
off still catches up.

```bash
systemctl list-timers wholesale-backup.timer
sudo systemctl start wholesale-backup.service      # run one now
ls -la /var/lib/wholesale/backups/
```

**Secrets are excluded by default.** An archive contains the database, the
reports and `.env.example`; it does not contain `.env`. Adding it takes an
explicit `--include-secrets` — do that only for an archive you are about to
encrypt.

### Restore

```bash
sudo systemctl stop wholesale-web

sudo -u wholesale /opt/wholesale/venv/bin/python - <<'PY'
from pathlib import Path
from wholesale_engine.backup import restore_database
archive = sorted(Path("/var/lib/wholesale/backups").glob("*.zip"))[-1]
print("restoring", archive)
print("ok:", restore_database(archive, Path("/var/lib/wholesale/leads.db")))
PY

sudo systemctl start wholesale-web
```

Test a restore into a scratch path *before* you need one. A backup you have
never restored is a hope, not a backup.

---

## Operating it

```bash
sudo systemctl status wholesale-web
sudo systemctl restart wholesale-web
sudo journalctl -u wholesale-web -f              # follow
sudo journalctl -u wholesale-web --since "1 hour ago"
sudo journalctl -u wholesale-hunt -n 200         # what the last hunt did
systemctl list-timers 'wholesale-*'
```

Health check: `curl -s http://127.0.0.1:8000/healthz` — touches no database, so
it stays honest about the process rather than about the data.

### Updating

```bash
cd /opt/wholesale/wholesale-code
sudo git pull
sudo /opt/wholesale/venv/bin/pip install -r wholesale_engine/requirements.txt
sudo systemctl restart wholesale-web
```

Your database, buy box and cache are in `/var/lib/wholesale` and are not touched
by a pull.

---

## What must never be exposed

| Never | Why |
|---|---|
| `tailscale funnel` | Publishes to the public internet. `serve` is the one you want. |
| `bind = 0.0.0.0` | Removes the only protection that cannot be mis-configured. |
| Port 8000 in a cloud security group | Same thing, one layer further out. |
| `.env` in a backup you then move around | It holds your API keys. |
| Reverse proxy on 80/443 without auth | A proxy is not a login. |

If you ever want this reachable without Tailscale, **add authentication
first**. Adding a login is a milestone of its own, not a config change.
