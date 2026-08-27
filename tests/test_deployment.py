"""Deployment tests: paths, units, and the WSGI entry point.

The failure this file mostly exists to prevent is a quiet one. A service whose
working directory differs from the checkout creates a *second, empty* database
beside the real one, serves a dashboard with nothing on it, and looks exactly
like data loss. Every path the engine writes to is therefore absolute and
resolved from the package, and a relative override is ignored rather than
honoured.

Nothing here starts a server or touches a network.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.util
import shutil
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wholesale_engine import paths

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = (
    "wholesale-web.service", "wholesale-backup.service", "wholesale-backup.timer",
    "wholesale-hunt.service", "wholesale-hunt.timer",
)


class DataDirOverride(unittest.TestCase):
    """``WHOLESALE_DATA_DIR`` moves the data, and nothing else does."""

    def setUp(self) -> None:
        self._saved = os.environ.get(paths.DATA_DIR_VAR)

    def tearDown(self) -> None:
        os.environ.pop(paths.DATA_DIR_VAR, None)
        if self._saved is not None:
            os.environ[paths.DATA_DIR_VAR] = self._saved
        importlib.reload(paths)

    def set_dir(self, value: str):
        os.environ[paths.DATA_DIR_VAR] = value
        importlib.reload(paths)
        return paths

    def test_the_default_is_inside_the_package(self):
        os.environ.pop(paths.DATA_DIR_VAR, None)
        importlib.reload(paths)
        self.assertEqual(paths.data_dir(), paths.PACKAGE_ROOT / "data")

    def test_an_absolute_override_moves_everything_together(self):
        p = self.set_dir("/var/lib/wholesale")
        self.assertEqual(p.database_path(), Path("/var/lib/wholesale/leads.db"))
        self.assertEqual(p.cache_dir(), Path("/var/lib/wholesale/cache"))
        self.assertEqual(p.ledger_path(), Path("/var/lib/wholesale/api_usage.json"))
        self.assertEqual(p.config_dir(), Path("/var/lib/wholesale/config"))

    def test_a_relative_override_is_ignored(self):
        # Resolving it against whatever the working directory happens to be is
        # the exact bug this module exists to prevent.
        p = self.set_dir("relative/data")
        self.assertEqual(p.data_dir(), p.PACKAGE_ROOT / "data")

    def test_a_blank_override_falls_back_to_the_default(self):
        p = self.set_dir("   ")
        self.assertEqual(p.data_dir(), p.PACKAGE_ROOT / "data")

    def test_every_path_is_absolute_whatever_the_override(self):
        for value in ("", "/srv/data", "nope/relative"):
            p = self.set_dir(value)
            for resolved in (p.data_dir(), p.database_path(), p.cache_dir(),
                             p.ledger_path(), p.config_dir()):
                self.assertTrue(resolved.is_absolute(), f"{value} -> {resolved}")


class NoSecondDatabase(unittest.TestCase):
    """The working directory must not be able to move the database."""

    def resolve_from(self, cwd: str, data_dir: str = "") -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env.pop(paths.DATA_DIR_VAR, None)
        if data_dir:
            env[paths.DATA_DIR_VAR] = data_dir
        return subprocess.run(
            [sys.executable, "-c",
             "from wholesale_engine.storage.database import DEFAULT_DB_PATH;"
             "print(DEFAULT_DB_PATH)"],
            cwd=cwd, env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_the_database_path_does_not_follow_the_working_directory(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            self.assertEqual(self.resolve_from(one), self.resolve_from(two))

    def test_the_override_wins_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as data:
            self.assertEqual(
                self.resolve_from(cwd, data), str(Path(data) / "leads.db")
            )

    def test_a_relocated_run_writes_nowhere_near_the_package(self):
        # The end-to-end version: run a real hunt with the override set and
        # confirm the package's own data directory gained nothing.
        repo = Path(__file__).resolve().parent.parent
        package_data = repo / "wholesale_engine" / "data"
        before = {p.name for p in package_data.iterdir()} if package_data.exists() else set()

        with tempfile.TemporaryDirectory() as data:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo)
            env[paths.DATA_DIR_VAR] = data
            subprocess.run(
                [sys.executable, "-m", "wholesale_engine.main", "--hunt",
                 "--source", "csv", "--quiet", "--out-dir", str(Path(data) / "out")],
                cwd=data, env=env, capture_output=True, text=True, check=True,
            )
            self.assertTrue((Path(data) / "leads.db").exists(), "wrote nothing")

        after = {p.name for p in package_data.iterdir()} if package_data.exists() else set()
        self.assertEqual(before, after, "the run wrote into the package directory")


class SystemdUnits(unittest.TestCase):
    def parse(self, name: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        parser.optionxform = str
        parser.read(DEPLOY / name)
        return parser

    def test_every_unit_exists_and_parses(self):
        for name in UNITS:
            self.assertTrue((DEPLOY / name).exists(), name)
            self.assertTrue(self.parse(name).sections(), name)

    def test_no_unit_runs_as_root(self):
        for name in UNITS:
            unit = self.parse(name)
            if not unit.has_section("Service"):
                continue
            self.assertEqual(unit["Service"].get("User"), "wholesale", name)

    def test_no_unit_contains_a_secret(self):
        # Credentials belong in the 0640 EnvironmentFile. A unit in
        # /etc/systemd/system is world-readable and shows in `systemctl cat`.
        for name in UNITS:
            body = (DEPLOY / name).read_text()
            for marker in ("API_KEY=", "PASSWORD=", "SECRET=", "TOKEN="):
                for line in body.splitlines():
                    if line.strip().startswith("#"):
                        continue
                    self.assertNotIn(marker, line, f"{name}: {line}")

    def test_services_load_the_environment_file(self):
        for name in ("wholesale-web.service", "wholesale-hunt.service",
                     "wholesale-backup.service"):
            self.assertEqual(
                self.parse(name)["Service"].get("EnvironmentFile"),
                "/etc/wholesale/env", name,
            )

    def test_the_web_service_restarts_on_failure(self):
        service = self.parse("wholesale-web.service")["Service"]
        self.assertEqual(service.get("Restart"), "on-failure")
        self.assertTrue(service.get("RestartSec"))

    def test_the_web_service_waits_for_the_data_directory(self):
        # Starting without it would create an empty database and serve a
        # dashboard with no leads on it.
        self.assertEqual(
            self.parse("wholesale-web.service")["Unit"].get("ConditionPathIsDirectory"),
            "/var/lib/wholesale",
        )

    def test_the_web_service_starts_after_tailscale(self):
        after = self.parse("wholesale-web.service")["Unit"].get("After", "")
        self.assertIn("tailscaled.service", after)

    def test_the_web_service_is_hardened(self):
        service = self.parse("wholesale-web.service")["Service"]
        for key, value in (
            ("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"),
            ("ProtectHome", "yes"), ("PrivateTmp", "yes"),
        ):
            self.assertEqual(service.get(key), value, key)
        self.assertEqual(service.get("ReadWritePaths"), "/var/lib/wholesale")

    def test_the_units_log_to_journald(self):
        for name in ("wholesale-web.service", "wholesale-hunt.service",
                     "wholesale-backup.service"):
            service = self.parse(name)["Service"]
            self.assertEqual(service.get("StandardOutput"), "journal", name)
            self.assertTrue(service.get("SyslogIdentifier"), name)

    def test_the_backup_unit_does_not_include_secrets(self):
        body = (DEPLOY / "wholesale-backup.service").read_text()
        for line in body.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("--include-secrets", line)

    def test_the_timers_survive_a_powered_off_vps(self):
        for name in ("wholesale-backup.timer", "wholesale-hunt.timer"):
            self.assertEqual(self.parse(name)["Timer"].get("Persistent"), "true", name)

    def test_only_the_hunt_unit_can_spend_money(self):
        # The dashboard and the backup must never make a provider request.
        for name in ("wholesale-web.service", "wholesale-backup.service"):
            self.assertNotIn("--hunt", (DEPLOY / name).read_text(), name)


class BindAddress(unittest.TestCase):
    def test_gunicorn_binds_loopback_by_default(self):
        sys.path.insert(0, str(DEPLOY.parent))
        try:
            spec = importlib.util.spec_from_file_location(
                "gconf", DEPLOY / "gunicorn.conf.py"
            )
            module = importlib.util.module_from_spec(spec)
            saved = os.environ.pop("WEB_BIND", None)
            try:
                spec.loader.exec_module(module)
                self.assertEqual(module.bind, "127.0.0.1:8000")
            finally:
                if saved is not None:
                    os.environ["WEB_BIND"] = saved
        finally:
            sys.path.pop(0)

    def test_no_deploy_file_sets_a_bind_to_all_interfaces(self):
        # 0.0.0.0 on an app with no authentication puts owner records on the
        # public internet. Checked against parsed VALUES rather than raw text,
        # so prose warning against it — which is most of where the string
        # appears — does not read as a setting.
        import ast

        for path in sorted(DEPLOY.iterdir()):
            if not path.is_file() or path.suffix == ".md":
                continue
            if path.suffix == ".py":
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        # A docstring is prose; a short literal is a setting.
                        if len(node.value) < 200:
                            self.assertNotIn("0.0.0.0", node.value, path.name)
            else:
                for line in path.read_text().splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith("*"):
                        continue
                    self.assertNotIn("0.0.0.0", stripped, f"{path.name}: {line}")

    def test_the_runbook_warns_against_binding_all_interfaces(self):
        self.assertIn("0.0.0.0", (DEPLOY / "README.md").read_text())

    def test_the_runbook_forbids_funnel(self):
        # `tailscale funnel` publishes to the public internet; `serve` does not.
        readme = (DEPLOY / "README.md").read_text()
        self.assertIn("tailscale serve", readme)
        self.assertIn("funnel", readme)
        self.assertIn("public internet", readme)

    def test_the_runbook_documents_the_lack_of_authentication(self):
        readme = (DEPLOY / "README.md").read_text()
        self.assertIn("no login", readme.lower())
        self.assertIn("Proving the network path", readme)


class WsgiEntryPoint(unittest.TestCase):
    def test_it_exposes_a_wsgi_application(self):
        sys.path.insert(0, str(DEPLOY.parent))
        try:
            import deploy.wsgi as wsgi

            importlib.reload(wsgi)
            self.assertTrue(callable(wsgi.application))
            self.assertIs(wsgi.app, wsgi.application)
        finally:
            sys.path.pop(0)

    def test_it_serves_without_a_network(self):
        sys.path.insert(0, str(DEPLOY.parent))
        try:
            import deploy.wsgi as wsgi

            importlib.reload(wsgi)
            response = wsgi.application.test_client().get("/healthz")
            self.assertEqual(response.status_code, 200)
        finally:
            sys.path.pop(0)

    def test_it_never_runs_the_development_server(self):
        body = (DEPLOY / "wsgi.py").read_text()
        self.assertNotIn(".run(", body)
        self.assertNotIn("debug=True", body)


class InstallScript(unittest.TestCase):
    def test_it_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY / "install.sh")], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_refuses_to_run_without_root(self):
        self.assertIn("id -u", (DEPLOY / "install.sh").read_text())

    def test_it_does_not_start_anything_listening(self):
        # Enabling a service is the operator's decision, made after they have
        # filled in credentials and proved the network path. The script may
        # PRINT the command as a next step; it must not run it. Everything
        # before the closing instructions heredoc is what actually executes.
        body = (DEPLOY / "install.sh").read_text()
        executed = body.split("cat <<'NEXT'")[0]
        for line in executed.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("systemctl enable", line)
            self.assertNotIn("systemctl start", line)
        self.assertIn("systemctl daemon-reload", executed)

    def test_it_tells_the_operator_what_is_left_to_do(self):
        instructions = (DEPLOY / "install.sh").read_text().split("cat <<'NEXT'")[1]
        self.assertIn("systemctl enable --now", instructions)
        self.assertIn("tailscale", instructions)
        self.assertIn("PROVE", instructions)

    def test_it_creates_the_data_directory_without_world_access(self):
        body = (DEPLOY / "install.sh").read_text()
        self.assertIn("0750", body)


class DependenciesDeclared(unittest.TestCase):
    def test_gunicorn_is_declared(self):
        requirements = (
            Path(__file__).resolve().parent.parent
            / "wholesale_engine" / "requirements.txt"
        ).read_text()
        self.assertIn("gunicorn>=23.0,<27.0", requirements)
        self.assertIn("Flask>=3.0,<4.0", requirements)

    def test_the_engine_still_needs_neither(self):
        # A VPS running only scheduled hunts installs nothing. Checked in a
        # fresh interpreter: by this point in a full test run something else
        # has already imported Flask, so sys.modules in THIS process says
        # nothing about what the CLI path pulls in.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, wholesale_engine.main, wholesale_engine.hunt;"
             "print([m for m in sys.modules "
             "if m.split('.')[0] in ('flask', 'gunicorn', 'werkzeug', 'jinja2')])"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "[]", result.stdout)


# ---------------------------------------------------------------------------
# Audit fix 1 — an unsafe production bind is refused, not merely discouraged
# ---------------------------------------------------------------------------


class ProductionBindIsGuarded(unittest.TestCase):
    """The dashboard has no authentication, so the bind IS the security control."""

    UNSAFE = (
        "0.0.0.0:8000",        # every IPv4 interface
        "[::]:8000",           # every IPv6 interface
        ":::8000",             # the same, unbracketed
        "192.168.1.10:8000",   # a LAN interface
        "203.0.113.9:8000",    # a public interface
        "10.0.0.5:8000",
        "8000",                # a bare port: gunicorn reads this as 0.0.0.0
        ":8000",
        "example.com:8000",    # a name that is not localhost
        "0:8000",
        "", "   ",             # unparseable fails closed, not open
    )
    SAFE = (
        "127.0.0.1:8000", "localhost:8000", "[::1]:8000",
        "127.0.0.53:9000",     # all of 127.0.0.0/8 is loopback
        "unix:/run/wholesale.sock",   # not on the network at all
    )

    def test_every_unsafe_bind_is_rejected(self):
        from wholesale_engine.web.bind import UnsafeBind, validate_bind

        for spec in self.UNSAFE:
            with self.assertRaises(UnsafeBind, msg=f"accepted {spec!r}"):
                validate_bind(spec)

    def test_loopback_and_unix_sockets_are_accepted(self):
        from wholesale_engine.web.bind import validate_bind

        for spec in self.SAFE:
            self.assertEqual(validate_bind(spec), spec)

    def test_the_refusal_explains_the_consequence_and_the_fix(self):
        from wholesale_engine.web.bind import UnsafeBind, validate_bind

        with self.assertRaises(UnsafeBind) as caught:
            validate_bind("0.0.0.0:8000")
        message = str(caught.exception)
        self.assertIn("NO AUTHENTICATION", message)
        self.assertIn("tailscale serve", message)
        self.assertIn("funnel", message)

    def test_the_gunicorn_config_fails_closed_on_an_unsafe_bind(self):
        # The whole point of the fix: a line in /etc/wholesale/env must not be
        # able to put owner records on the internet. Loading the config must
        # raise, so gunicorn never starts and systemd reports the failure.
        from wholesale_engine.web.bind import UnsafeBind

        saved = os.environ.get("WEB_BIND")
        os.environ["WEB_BIND"] = "0.0.0.0:8000"
        try:
            spec = importlib.util.spec_from_file_location(
                "gconf_unsafe", DEPLOY / "gunicorn.conf.py"
            )
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(UnsafeBind):
                spec.loader.exec_module(module)
        finally:
            os.environ.pop("WEB_BIND", None)
            if saved is not None:
                os.environ["WEB_BIND"] = saved

    def test_the_gunicorn_config_accepts_a_loopback_override(self):
        saved = os.environ.get("WEB_BIND")
        os.environ["WEB_BIND"] = "127.0.0.1:9999"
        try:
            spec = importlib.util.spec_from_file_location(
                "gconf_safe", DEPLOY / "gunicorn.conf.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.bind, "127.0.0.1:9999")
        finally:
            os.environ.pop("WEB_BIND", None)
            if saved is not None:
                os.environ["WEB_BIND"] = saved

    def test_gunicorn_actually_exits_non_zero_on_an_unsafe_bind(self):
        # Not just "the import raises" — the real binary must refuse to serve.
        repo = Path(__file__).resolve().parent.parent
        env = dict(os.environ, WEB_BIND="0.0.0.0:8000")
        result = subprocess.run(
            [sys.executable, "-m", "gunicorn", "--config",
             str(DEPLOY / "gunicorn.conf.py"), "deploy.wsgi:application"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NO AUTHENTICATION", result.stdout + result.stderr)

    def test_the_dev_server_uses_the_same_guard(self):
        # One implementation. Two would mean one of them drifts.
        from wholesale_engine.web import app as web_app
        from wholesale_engine.web.bind import UnsafeBind

        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.assertRaises(UnsafeBind, msg=host):
                web_app.run_dev_server(host=host)

    def test_the_runbook_does_not_suggest_widening_the_bind(self):
        readme = (DEPLOY / "README.md").read_text()
        self.assertNotIn("WEB_BIND=0.0.0.0", readme)


# ---------------------------------------------------------------------------
# Audit fix 2 — the documented restore actually restores
# ---------------------------------------------------------------------------


class DocumentedRestoreWorks(unittest.TestCase):
    """Runs the code taken out of the runbook, in a VPS-shaped environment."""

    def restore_snippet(self) -> str:
        """The python the runbook tells you to run for a dry-run restore."""
        readme = (DEPLOY / "README.md").read_text()
        marker = "print(\"leads restored:\", sqlite3.connect(target).execute("
        self.assertIn(marker, readme, "the runbook's dry-run block moved")
        block = readme.split("**First, dry-run it into a scratch file.**")[1]
        body = block.split("/opt/wholesale/venv/bin/python -c '")[1].split("'\n```")[0]
        return body

    def test_the_runbook_sets_pythonpath(self):
        # The package is not pip-installed into the venv, and a restore is run
        # from wherever the operator happens to be standing.
        readme = (DEPLOY / "README.md").read_text()
        restore = readme.split("### Restore")[1]
        self.assertIn("PYTHONPATH=/opt/wholesale/wholesale-code", restore)
        self.assertIn("ModuleNotFoundError", restore)

    def test_the_package_is_not_pip_installable_so_pythonpath_is_required(self):
        repo = Path(__file__).resolve().parent.parent
        for name in ("pyproject.toml", "setup.py", "setup.cfg"):
            self.assertFalse((repo / name).exists(), f"{name} appeared — revisit the runbook")

    def test_backup_then_restore_into_an_empty_destination(self):
        """backup -> fresh destination -> restore -> opens -> records present."""
        import sqlite3

        repo = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, fresh = root / "data", root / "fresh"
            data.mkdir()
            fresh.mkdir()

            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo)
            env[paths.DATA_DIR_VAR] = str(data)

            subprocess.run(
                [sys.executable, "-m", "wholesale_engine.main", "--hunt",
                 "--source", "csv", "--quiet", "--out-dir", str(root / "out")],
                cwd=tmp, env=env, capture_output=True, text=True, check=True,
            )
            seeded = sqlite3.connect(data / "leads.db").execute(
                "SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertGreater(seeded, 0)

            subprocess.run(
                [sys.executable, "-m", "wholesale_engine.main", "--backup",
                 "--backup-dir", str(data / "backups"), "--quiet"],
                cwd=tmp, env=env, capture_output=True, text=True, check=True,
            )

            # Run the runbook's own code, from a working directory that is not
            # the checkout — which is what broke before this fix.
            target = fresh / "leads.db"
            self.assertFalse(target.exists())
            snippet = self.restore_snippet()
            snippet = snippet.replace("/var/lib/wholesale/backups", str(data / "backups"))
            snippet = snippet.replace("/tmp/restore-check.db", str(target))
            result = subprocess.run(
                [sys.executable, "-c", snippet],
                cwd="/", env={"PYTHONPATH": str(repo), "PATH": os.environ.get("PATH", "")},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok: True", result.stdout)

            self.assertTrue(target.exists(), "restore produced no database")
            restored = sqlite3.connect(target).execute(
                "SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertEqual(restored, seeded)
            self.assertIn(f"leads restored: {seeded}", result.stdout)

    def test_the_old_broken_form_would_have_failed(self):
        # Guards the fix rather than the symptom: without PYTHONPATH, from a
        # foreign directory, the import genuinely does not resolve.
        result = subprocess.run(
            [sys.executable, "-c", "from wholesale_engine.backup import restore_database"],
            cwd="/", env={"PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError", result.stderr)


# ---------------------------------------------------------------------------
# Audit fix 3 — restart limits where systemd actually reads them
# ---------------------------------------------------------------------------


class RestartLimitSection(unittest.TestCase):
    def test_start_limits_are_in_the_unit_section(self):
        parser = configparser.ConfigParser(strict=False, allow_no_value=True)
        parser.optionxform = str
        parser.read(DEPLOY / "wholesale-web.service")
        self.assertEqual(parser["Unit"].get("StartLimitIntervalSec"), "60")
        self.assertEqual(parser["Unit"].get("StartLimitBurst"), "5")
        self.assertIsNone(parser["Service"].get("StartLimitIntervalSec"))
        self.assertIsNone(parser["Service"].get("StartLimitBurst"))

    def test_systemd_analyze_reports_no_unknown_keys(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze unavailable")
        for name in UNITS:
            result = subprocess.run(
                ["systemd-analyze", "verify", f"./{name}"],
                cwd=str(DEPLOY), capture_output=True, text=True,
            )
            output = result.stdout + result.stderr
            # Missing executables and tailscaled are expected off the VPS;
            # an unknown key name never is.
            self.assertNotIn("Unknown key name", output, f"{name}: {output}")
            self.assertNotIn("Unknown section", output, f"{name}: {output}")


# ---------------------------------------------------------------------------
# Audit fix 4 — credential files are named, not blanket-chmodded
# ---------------------------------------------------------------------------


class CredentialPermissions(unittest.TestCase):
    def script(self) -> str:
        return (DEPLOY / "install.sh").read_text()

    def test_the_installer_locks_down_a_checkout_env(self):
        body = self.script()
        self.assertIn('"$REPO_DIR/.env"', body)
        self.assertIn("chmod 0600", body)

    def test_it_does_not_blanket_chmod_the_repository(self):
        # 0600 across the tree would make the code unreadable to the service
        # user, which is a different outage with the same cause.
        for line in self.script().splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("chmod -R 0600", line)
            self.assertNotIn("chmod -R 600", line)

    def test_the_environment_file_permissions_are_explicit(self):
        body = self.script()
        self.assertIn('chown "root:$APP_USER" "$ENV_FILE"', body)
        self.assertIn('chmod 0640 "$ENV_FILE"', body)

    def test_it_never_edits_credential_contents(self):
        # Permissions only. A guarded heredoc creates the file when absent;
        # nothing rewrites one that exists.
        body = self.script()
        self.assertIn('if [ ! -f "$ENV_FILE" ]', body)
        for line in body.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("sed -i", line)
            self.assertNotIn('> "$REPO_DIR/.env"', line)

    def test_go_w_alone_would_have_left_a_checkout_env_readable(self):
        # The reason the explicit chmod exists: -R go-w removes write, not read.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".env"
            target.write_text("RENTCAST_API_KEY=secret\n")
            target.chmod(0o644)
            subprocess.run(["chmod", "-R", "go-w", tmp], check=True)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)


# ---------------------------------------------------------------------------
# Audit fix 5 — no test tooling on the production VPS
# ---------------------------------------------------------------------------


class ProductionDependencies(unittest.TestCase):
    def requirements(self, name: str) -> list:
        text = (
            Path(__file__).resolve().parent.parent / "wholesale_engine" / name
        ).read_text()
        return [
            line.split("#")[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_production_installs_only_flask_and_gunicorn(self):
        self.assertEqual(
            self.requirements("requirements.txt"),
            ["Flask>=3.0,<4.0", "gunicorn>=23.0,<27.0"],
        )

    def test_pytest_is_not_a_production_dependency(self):
        for entry in self.requirements("requirements.txt"):
            self.assertNotIn("pytest", entry)

    def test_the_dev_file_exists_and_includes_production(self):
        entries = self.requirements("requirements-dev.txt")
        self.assertIn("-r requirements.txt", entries)
        self.assertTrue(any("pytest" in e for e in entries))

    def test_the_installer_uses_the_production_file(self):
        body = (DEPLOY / "install.sh").read_text()
        self.assertIn("requirements.txt", body)
        self.assertNotIn("requirements-dev.txt", body)

    def test_the_suite_needs_neither(self):
        # unittest, standard library. Splitting the files must not have made
        # the tests depend on something a fresh clone lacks.
        self.assertNotIn("pytest", sys.modules)


# ---------------------------------------------------------------------------
# Audit fix 6 — the installer fails closed on a dangerous REPO_DIR
# ---------------------------------------------------------------------------


class RepoDirGuard(unittest.TestCase):
    def run_installer(self, repo_dir):
        env = dict(os.environ)
        if repo_dir is None:
            env.pop("REPO_DIR", None)
        else:
            env["REPO_DIR"] = repo_dir
        # Point everything else at a path that cannot exist, so that even if a
        # guard failed the run would stop before touching anything real.
        env.update({"INSTALL_DIR": "/nonexistent-install",
                    "DATA_DIR": "/nonexistent-data",
                    "ENV_FILE": "/nonexistent/env"})
        return subprocess.run(
            ["bash", str(DEPLOY / "install.sh")],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def test_the_filesystem_root_is_refused(self):
        result = self.run_installer("/")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REFUSING TO RUN", result.stderr)
        self.assertIn("filesystem root", result.stderr)

    def test_an_empty_repo_dir_is_refused(self):
        result = self.run_installer("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REPO_DIR is empty", result.stderr)

    def test_a_relative_repo_dir_is_refused(self):
        result = self.run_installer("relative/path")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute path", result.stderr)

    def test_system_directories_are_refused(self):
        for candidate in ("/etc", "/usr", "/var", "/home", "/root"):
            result = self.run_installer(candidate)
            self.assertNotEqual(result.returncode, 0, candidate)
            self.assertIn("system directory", result.stderr, candidate)

    def test_a_directory_that_is_not_this_project_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_installer(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a checkout of this project", result.stderr)

    def test_a_partial_checkout_is_refused(self):
        # Looks plausible, missing the file that matters.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "wholesale_engine").mkdir()
            (Path(tmp) / "wholesale_engine" / "__init__.py").write_text("")
            result = self.run_installer(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a checkout", result.stderr)

    def test_a_traversal_in_the_path_is_refused(self):
        result = self.run_installer("/opt/wholesale/../../etc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REFUSING TO RUN", result.stderr)

    def test_the_guard_runs_before_any_recursive_operation(self):
        # Compared over EXECUTED lines only. The script's own comments mention
        # `chown -R` while explaining why the guard exists, and matching prose
        # would make this pass or fail for the wrong reason.
        lines = [
            line for line in (DEPLOY / "install.sh").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        guard = next(i for i, l in enumerate(lines) if "REFUSING TO RUN" in l)
        recursive = next(i for i, l in enumerate(lines) if l.strip().startswith("chown -R"))
        self.assertLess(guard, recursive, "the guard must come before the recursive chown")


class UnitOverwriteBehaviour(unittest.TestCase):
    def test_a_customised_unit_is_backed_up_before_being_replaced(self):
        body = (DEPLOY / "install.sh").read_text()
        self.assertIn("cmp -s", body)
        self.assertIn(".bak-", body)

    def test_the_behaviour_is_documented(self):
        readme = (DEPLOY / "README.md").read_text()
        self.assertIn("Re-running the installer", readme)


if __name__ == "__main__":
    unittest.main()
