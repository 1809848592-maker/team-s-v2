"""Offline trial configuration/host launcher tests; python3 deploy/test_trial.py."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class TrialConfigurationTests(unittest.TestCase):
    def config(self, **values):
        environment = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("DEV_LAN_")
            },
            "DEEPSEEK_API_KEY": "",
            **values,
        }
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "-p",
                "teams-platform-dev",
                "-f",
                str(ROOT / "docker-compose.yml"),
                "-f",
                str(ROOT / "docker-compose.trial.yml"),
                "config",
                "--format",
                "json",
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_default_lan_disabled_and_web_stays_loopback_without_extra_services(self):
        config = self.config()
        self.assertEqual(config.get("x-dev-lan", {}).get("enabled"), "false")
        self.assertEqual(set(config["services"]), {"postgres", "web", "worker"})
        self.assertEqual(config["services"]["web"]["ports"][0]["host_ip"], "127.0.0.1")
        for name in ("web", "worker"):
            env = config["services"][name]["environment"]
            self.assertEqual(env["LLM_PROJECT_DAILY_REVIEWS"], "10")
            self.assertNotIn("192.168.9.3", env["MCP_ALLOWED_HOSTS"])

    def test_only_trial_adds_exact_lan_authority_and_origin_without_wildcard(self):
        config = self.config(
            DEV_LAN_ENABLED="true",
            DEV_LAN_IP="192.168.9.3",
            DEV_LAN_ENDPOINT="192.168.9.3:8123",
            WEB_PORT="8123",
        )
        self.assertEqual(config.get("x-dev-lan", {}).get("enabled"), "true")
        self.assertEqual(config["x-dev-lan"]["ip"], "192.168.9.3")
        for name in ("web", "worker"):
            env = config["services"][name]["environment"]
            self.assertIn("192.168.9.3:8123", env["MCP_ALLOWED_HOSTS"].split(","))
            self.assertIn(
                "http://192.168.9.3:8123", env["MCP_ALLOWED_ORIGINS"].split(",")
            )
            self.assertNotIn("192.168.9.3:*", env["MCP_ALLOWED_HOSTS"])
            self.assertIn("localhost:*", env["MCP_ALLOWED_HOSTS"].split(","))
        self.assertEqual(config["services"]["web"]["ports"][0]["host_ip"], "127.0.0.1")


class TrialLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "deploy/trial.py"
        spec = importlib.util.spec_from_file_location("trial_launcher", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def config(self, enabled="true", ip="192.168.9.3"):
        return {
            "name": "teams-platform-dev",
            "x-dev-lan": {"enabled": enabled, "ip": ip, "colima_profile": "teams-m1"},
            "services": {
                "web": {
                    "ports": [
                        {
                            "host_ip": "127.0.0.1",
                            "published": "8000",
                            "target": 8000,
                            "protocol": "tcp",
                        }
                    ]
                }
            },
        }

    def test_enabled_requires_local_private_ipv4_and_strict_boolean(self):
        for ip in (
            "",
            "0.0.0.0",
            "127.0.0.1",
            "8.8.8.8",
            "169.254.1.2",
            "::",
            "$(bad)",
        ):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                self.module.lan_settings(self.config(ip=ip))
        with self.assertRaises(ValueError):
            self.module.lan_settings(self.config(enabled="yes"))
        with patch.object(self.module.socket, "socket") as sock:
            sock.return_value.__enter__.return_value.bind.side_effect = OSError(
                "not local"
            )
            with self.assertRaises(OSError):
                self.module.lan_settings(self.config())

    def test_disabled_drops_inherited_lan_endpoint_and_key_from_terminal(self):
        env = self.module.trial_environment(
            {
                "PATH": "tools",
                "DEEPSEEK_API_KEY": "wrong-key",
                "DEV_LAN_ENDPOINT": "8.8.8.8:80",
            }
        )
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["DEV_LAN_ENDPOINT"], "")
        self.assertEqual(self.module.lan_settings(self.config(enabled="false")), None)

    def test_start_binds_only_requested_ip_uses_own_control_socket_and_no_global_change(
        self,
    ):
        calls = []

        def run(command, **options):
            calls.append(command)
            output = "Host colima-teams-m1\n  HostName 127.0.0.1\n  Port 53362\n"
            if command[:2] == ["docker", "info"] or "ssh" in command:
                output = "same-daemon\n"
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with patch.object(self.module.subprocess, "run", side_effect=run):
            settings = {"ip": "192.168.9.3", "port": 8000, "profile": "teams-m1"}
            self.module.start_forwarder(
                self.state,
                settings,
                {},
                self.module.colima_connection(settings, {}),
            )
        ssh = next(command for command in calls if command[0] == "ssh")
        self.assertIn("192.168.9.3:8000:127.0.0.1:8000", ssh)
        self.assertIn(str(self.state / "control.sock"), ssh)
        self.assertIn("ExitOnForwardFailure=yes", ssh)
        self.assertIn("ControlPersist=no", ssh)
        self.assertNotIn("0.0.0.0", " ".join(ssh))
        self.assertFalse(any(command[:2] == ["colima", "start"] for command in calls))

    def test_mismatched_colima_daemon_refuses_to_create_listener(self):
        calls = []

        def run(command, **options):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="mac-daemon" if command[0] == "docker" else "other-daemon",
            )

        with (
            patch.object(self.module.subprocess, "run", side_effect=run),
            self.assertRaises(ValueError),
        ):
            self.module.colima_connection(
                {"ip": "192.168.9.3", "port": 8000, "profile": "teams-m1"},
                {},
            )
        self.assertFalse(any(command[0] == "ssh" for command in calls))

    def test_close_only_exits_owned_control_socket(self):
        (self.state / "state.json").write_text(json.dumps({"host": "colima-teams-m1"}))
        (self.state / "ssh.conf").write_text("Host colima-teams-m1\n")
        (self.state / "control.sock").touch()

        def stopped(command, **options):
            (self.state / "control.sock").unlink()
            return subprocess.CompletedProcess(command, 0)

        with patch.object(self.module.subprocess, "run", side_effect=stopped) as run:
            self.module.stop_forwarder(self.state, {})
        command = run.call_args.args[0]
        self.assertIn(str(self.state / "control.sock"), command)
        self.assertEqual(command[-3:], ["-O", "exit", "colima-teams-m1"])
        self.assertFalse((self.state / "state.json").exists())

    def test_disabled_stops_listener_before_compose_and_failure_never_leaves_tunnel(
        self,
    ):
        operations = []

        def run(command, **options):
            operations.append(command)
            if command[-3:] == ["config", "--format", "json"]:
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(self.config(enabled="false"))
                )
            raise subprocess.CalledProcessError(1, command)

        with (
            patch.object(
                self.module,
                "stop_forwarder",
                side_effect=lambda *_: operations.append(["close"]),
            ),
            patch.object(self.module.subprocess, "run", side_effect=run),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.module.apply({}, self.state)
        up = next(i for i, c in enumerate(operations) if "up" in c)
        self.assertLess(operations.index(["close"]), up)
        self.assertTrue(
            all("postgres" not in c and "down" not in c for c in operations)
        )

    def test_close_happens_even_when_compose_configuration_cannot_be_read(self):
        operations = []

        def broken(command, **options):
            operations.append(["config failed"])
            raise subprocess.CalledProcessError(1, command)

        with (
            patch.object(
                self.module,
                "stop_forwarder",
                side_effect=lambda *_: operations.append(["closed"]),
            ),
            patch.object(self.module.subprocess, "run", side_effect=broken),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.module.apply({"DEV_LAN_ENABLED": "false"}, self.state)
        self.assertEqual(operations, [["closed"], ["config failed"]])

    def test_wrong_daemon_is_rejected_before_any_compose_service_change(self):
        calls = []

        def run(command, **options):
            calls.append(command)
            if command[-3:] == ["config", "--format", "json"]:
                output = json.dumps(self.config())
            elif command[0] == "docker":
                output = "current-daemon"
            else:
                output = "different-colima-daemon"
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with (
            patch.object(self.module, "stop_forwarder"),
            patch.object(self.module.subprocess, "run", side_effect=run),
            patch.object(self.module.socket, "socket"),
            self.assertRaises(ValueError),
        ):
            self.module.apply({"DEV_LAN_ENABLED": "true"}, self.state)
        self.assertFalse(any("up" in command for command in calls), calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
