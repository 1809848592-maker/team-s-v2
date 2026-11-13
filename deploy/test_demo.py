"""Daemon-free launcher and Compose checks: python3 deploy/test_demo.py."""

import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DemoComposeTests(unittest.TestCase):
    def test_operator_keys_database_and_ports_cannot_override_demo_isolation(self):
        spec = importlib.util.spec_from_file_location("demo_launcher", ROOT / "deploy/demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        inherited = dict(
            os.environ,
            DATABASE_URL="postgresql://do-not-touch",
            DEEPSEEK_API_KEY="fixture-key-never-pass-through",
            LLM_ENABLE_LIVE_CALLS="true",
            WEB_PORT="8123",
            POSTGRES_DB="production",
        )
        safe = module.demo_environment(inherited, 18005)
        result = subprocess.run(
            module.compose_command() + ["config", "--format", "json"],
            env=safe,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config["name"], "teams-platform-demo")
        self.assertEqual(set(config["volumes"]), {"postgres_demo_data", "files_demo_data"})
        self.assertEqual(config["services"]["postgres"]["environment"]["POSTGRES_DB"], "teams_demo")
        self.assertNotIn("ports", config["services"]["postgres"])
        for service in ("web", "worker"):
            values = config["services"][service]["environment"]
            self.assertEqual(values["DEEPSEEK_API_KEY"], "")
            self.assertEqual(values["LLM_ENABLE_LIVE_CALLS"], "false")
            self.assertEqual(values["STORAGE_DIR"], "/app/storage/content")
            self.assertTrue(values["DATABASE_URL"].endswith("/teams_demo"))
            self.assertEqual(config["services"][service]["image"], "teams-platform:demo")
        self.assertEqual(config["services"]["web"]["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(config["services"]["web"]["ports"][0]["published"], "18005")
        self.assertNotIn("fixture-key-never-pass-through", result.stdout)
        self.assertNotIn("production", result.stdout)

    def test_invalid_ports_fail_before_docker_or_initialization(self):
        for value in ("0", "65536", "abc"):
            result = subprocess.run(
                ["python3", str(ROOT / "deploy/demo.py"), "--port", value],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("port", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
