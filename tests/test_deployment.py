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


if __name__ == "__main__":
    unittest.main()
