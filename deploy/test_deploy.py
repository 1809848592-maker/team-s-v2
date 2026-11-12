"""Run with python3 deploy/test_deploy.py; requires Docker Compose, not a daemon."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("maintenance", ROOT / "deploy/maintenance.py")
maintenance = importlib.util.module_from_spec(SPEC)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env_file = Path(self.temporary.name) / "production.env"
        self.values = {
            "PUBLIC_DOMAIN": "teams.example.org",
            "ACME_EMAIL": "operator@example.org",
            "POSTGRES_PASSWORD": "a" * 64,
        }

    def config(self):
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in self.values.items()))
        # Explicitly avoid the operator's real .env and any inherited model key.
        environment = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "TMPDIR"}
        }
        return subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(ROOT / "deploy"),
                "--env-file",
                str(self.env_file),
                "-f",
                str(ROOT / "deploy/compose.production.yaml"),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_only_https_proxy_is_public_and_state_is_persistent(self):
        result = self.config()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(set(config["services"]), {"postgres", "web", "worker", "caddy"})
        self.assertEqual(Path(config["services"]["web"]["build"]["context"]), ROOT)
        self.assertEqual(
            Path(config["services"]["caddy"]["volumes"][0]["source"]),
            ROOT / "deploy/Caddyfile",
        )
        for name in ("postgres", "web", "worker"):
            self.assertFalse(config["services"][name].get("ports"))
        ports = config["services"]["caddy"]["ports"]
        for name in ("NO_PROXY", "no_proxy"):
            self.assertIn("web", config["services"]["caddy"]["environment"][name].split(","))
        self.assertEqual(
            {(p["published"], p["protocol"]) for p in ports},
            {("80", "tcp"), ("443", "tcp"), ("443", "udp")},
        )
        for name in ("web", "worker"):
            service = config["services"][name]
            self.assertEqual(service["environment"]["SESSION_COOKIE_SECURE"], "true")
            self.assertEqual(service["environment"]["LLM_ENABLE_LIVE_CALLS"], "false")
            self.assertEqual(service["environment"]["DEEPSEEK_API_KEY"], "")
            self.assertEqual(service["environment"]["MCP_ALLOWED_HOSTS"], "teams.example.org")
            self.assertEqual(
                service["environment"]["MCP_ALLOWED_ORIGINS"],
                "https://teams.example.org",
            )
            self.assertEqual(service["restart"], "unless-stopped")
            self.assertTrue(all(v["type"] == "volume" for v in service["volumes"]))
            self.assertFalse(service.get("develop"))
        self.assertEqual(
            set(config["volumes"]),
            {"postgres_data", "files_data", "caddy_data", "caddy_config"},
        )

    def test_required_deployment_identity_and_password_have_no_dev_fallback(self):
        for key in tuple(self.values):
            with self.subTest(key=key):
                value = self.values.pop(key)
                result = self.config()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.values[key] = value

    def test_explicit_quotas_and_live_flag_reach_both_processes(self):
        self.values.update(
            {
                "LLM_ENABLE_LIVE_CALLS": "true",
                "DEEPSEEK_API_KEY": "fixture-not-a-real-key",
                "BOTTLENECK_RETURN_DAYS": "7",
                "LLM_PLATFORM_DAILY_TOKENS": "1234",
                "PLATFORM_ADMIN_USER_IDS": "9c7b93d3-690e-45ac-b57b-4701972b6c59",
            }
        )
        result = self.config()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        for name in ("web", "worker"):
            for key in self.values.keys() - {
                "PUBLIC_DOMAIN",
                "ACME_EMAIL",
                "POSTGRES_PASSWORD",
            }:
                self.assertEqual(config["services"][name]["environment"][key], self.values[key])


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SPEC.loader.exec_module(maintenance)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.backup = Path(self.temporary.name)
        (self.backup / "database.dump").write_bytes(b"PGDMP-fixture")
        with tarfile.open(self.backup / "storage.tar", "w") as archive:
            info = tarfile.TarInfo("ab/cd/content-addressed-file")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"test"))

    def seal(self):
        maintenance.seal_backup(self.backup, {"application_image": "fixture-sha256"})

    def test_complete_backup_verifies_bytes_and_carries_version_without_secrets(self):
        self.seal()
        manifest = maintenance.verify_backup(self.backup)
        self.assertEqual(manifest["metadata"]["application_image"], "fixture-sha256")
        self.assertEqual(
            manifest["sha256"]["database.dump"],
            hashlib.sha256(b"PGDMP-fixture").hexdigest(),
        )

    def test_corrupt_or_missing_payload_is_rejected_before_recovery(self):
        self.seal()
        (self.backup / "database.dump").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.backup)
        (self.backup / "storage.tar").unlink()
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.backup)

    def test_partial_backup_without_manifest_is_rejected(self):
        with self.assertRaises(ValueError):
            maintenance.verify_backup(self.backup)

    def test_storage_archive_cannot_escape_target_or_restore_links(self):
        for name, kind in (
            ("../outside", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("symlink", tarfile.SYMTYPE),
            ("hardlink", tarfile.LNKTYPE),
        ):
            with self.subTest(name=name):
                with tarfile.open(self.backup / "storage.tar", "w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "../../outside" if kind != tarfile.REGTYPE else ""
                    archive.addfile(info)
                self.seal()
                with self.assertRaises(ValueError):
                    maintenance.verify_backup(self.backup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
