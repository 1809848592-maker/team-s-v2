"""Daemon-free launcher and Compose checks: python3 deploy/test_demo.py."""

import importlib.util
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "teams-platform-demo"
VOLUMES = [PROJECT + "_postgres_demo_data", PROJECT + "_files_demo_data"]


def launcher():
    spec = importlib.util.spec_from_file_location(
        "demo_launcher", ROOT / "deploy/demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = launcher()
        result = subprocess.run(
            module.compose_command()
            + ["--profile", "seed", "config", "--format", "json"],
            env=module.demo_environment(os.environ, 18005),
            check=True,
            text=True,
            capture_output=True,
        )
        cls.safe_config = json.loads(result.stdout)

    def invoke(
        self, *args, answer=PROJECT, config=None, labels=None, absent=False, fail=None
    ):
        module = launcher()
        commands = []

        def docker(command, **options):
            commands.append(command)
            self.assertTrue(options["check"])
            self.assertNotIn("DEEPSEEK_API_KEY", options["env"])
            if fail and fail(command):
                raise subprocess.CalledProcessError(1, command)
            output = ""
            if command[-3:] == ["config", "--format", "json"]:
                output = json.dumps(config if config is not None else self.safe_config)
            elif command[:3] == ["docker", "volume", "ls"]:
                output = "\n".join(
                    [] if absent else VOLUMES + ["teams-platform-dev_postgres_data"]
                )
            elif command[:3] == ["docker", "volume", "inspect"]:
                output = json.dumps(
                    [
                        {
                            "Name": volume,
                            "Labels": labels
                            if labels is not None
                            else {
                                "com.docker.compose.project": PROJECT,
                                "com.docker.compose.volume": volume.removeprefix(
                                    PROJECT + "_"
                                ),
                            },
                        }
                        for volume in command[3:]
                    ]
                )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        stream = io.StringIO()
        with (
            patch.object(module.sys, "argv", ["demo.py", *args]),
            patch.object(module.subprocess, "run", side_effect=docker),
            patch(
                "builtins.input",
                side_effect=answer if isinstance(answer, BaseException) else None,
                return_value=answer,
            ) as prompt,
            patch.object(module.urllib.request, "build_opener") as opener,
            patch("sys.stdout", stream),
            patch("sys.stderr", stream),
        ):
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            try:
                status = module.main()
            except SystemExit as error:
                status = error.code
        return status, commands, stream.getvalue(), prompt.call_count

    def test_reset_requires_exact_confirmation_before_any_mutation(self):
        for answer in ("", "yes", "teams-platform-dev", EOFError()):
            with self.subTest(answer=repr(answer)):
                status, commands, output, prompts = self.invoke(
                    "--reset", answer=answer
                )
                self.assertNotEqual(status, 0)
                self.assertEqual(prompts, 1)
                self.assertTrue(all("config" in command for command in commands))
                self.assertNotIn("演示已就绪", output)
                self.assertIn("取消", output)

    def test_confirmed_reset_deletes_only_two_fixed_volumes_then_regenerates(self):
        status, commands, output, prompts = self.invoke("--reset")
        self.assertEqual(status, 0, output)
        self.assertEqual(prompts, 1)
        removals = [c for c in commands if c[:3] == ["docker", "volume", "rm"]]
        self.assertEqual(removals, [["docker", "volume", "rm", *VOLUMES]])
        down = next(i for i, c in enumerate(commands) if c[-1:] == ["down"])
        remove = commands.index(removals[0])
        seed = next(i for i, c in enumerate(commands) if c[-1:] == ["seed"])
        build = next(i for i, c in enumerate(commands) if c[-2:] == ["build", "web"])
        self.assertLess(build, down)
        self.assertLess(down, remove)
        self.assertLess(remove, seed)
        self.assertTrue(
            all(
                "--volumes" not in c and "prune" not in c and "--force" not in c
                for c in commands
            )
        )
        self.assertIn("演示已就绪", output)

    def test_reset_handles_a_fresh_environment_without_removing_other_volumes(self):
        status, commands, output, _ = self.invoke("--reset", "--no-build", absent=True)
        self.assertEqual(status, 0, output)
        self.assertFalse(any(c[:3] == ["docker", "volume", "rm"] for c in commands))
        self.assertTrue(any(c[-1:] == ["seed"] for c in commands))

    def test_reset_rejects_changed_volume_mapping_and_external_volumes(self):
        for change in ("name", "external", "extra", "mount"):
            config = json.loads(json.dumps(self.safe_config))
            if change == "name":
                config["volumes"]["postgres_demo_data"]["name"] = (
                    "teams-platform-dev_postgres_data"
                )
            elif change == "external":
                config["volumes"]["files_demo_data"]["external"] = True
            elif change == "extra":
                config["volumes"]["other"] = {"name": "other"}
            else:
                config["services"]["postgres"]["volumes"][0]["source"] = "other"
            with self.subTest(change=change):
                status, commands, output, _ = self.invoke("--reset", config=config)
                self.assertNotEqual(status, 0)
                self.assertTrue(all("config" in c for c in commands))
                self.assertNotIn("演示已就绪", output)

    def test_reset_rejects_volumes_not_owned_by_demo_before_stopping_services(self):
        for labels in ({}, {"com.docker.compose.project": "teams-platform-dev"}):
            status, commands, output, _ = self.invoke("--reset", labels=labels)
            self.assertNotEqual(status, 0)
            self.assertFalse(any(c[-1:] == ["down"] for c in commands))
            self.assertFalse(any(c[:3] == ["docker", "volume", "rm"] for c in commands))
            self.assertNotIn("演示已就绪", output)

    def test_reset_failures_do_not_seed_or_claim_that_deleted_volumes_were_preserved(
        self,
    ):
        for fail in (
            lambda c: c[-1:] == ["down"],
            lambda c: c[:3] == ["docker", "volume", "rm"],
        ):
            status, commands, output, _ = self.invoke("--reset", fail=fail)
            self.assertNotEqual(status, 0)
            self.assertFalse(any(c[-1:] == ["seed"] for c in commands))
            self.assertNotIn("演示已就绪", output)
            self.assertNotIn("卷未删除", output)
            self.assertIn("重置", output)

    def test_stop_and_reset_are_mutually_exclusive(self):
        status, commands, _, prompts = self.invoke("--reset", "--stop")
        self.assertNotEqual(status, 0)
        self.assertEqual(commands, [])
        self.assertEqual(prompts, 0)

    def test_normal_launch_and_stop_never_delete_volumes_or_prompt(self):
        for args in (("--no-build",), ("--stop",)):
            status, commands, output, prompts = self.invoke(*args)
            self.assertEqual(status, 0, output)
            self.assertEqual(prompts, 0)
            self.assertFalse(
                any(
                    c[:2] == ["docker", "volume"] or c[-1:] == ["down"]
                    for c in commands
                )
            )


class DemoComposeTests(unittest.TestCase):
    def test_operator_keys_database_and_ports_cannot_override_demo_isolation(self):
        spec = importlib.util.spec_from_file_location(
            "demo_launcher", ROOT / "deploy/demo.py"
        )
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
            check=False,
            env=safe,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config["name"], "teams-platform-demo")
        self.assertEqual(
            set(config["volumes"]), {"postgres_demo_data", "files_demo_data"}
        )
        self.assertEqual(
            config["services"]["postgres"]["environment"]["POSTGRES_DB"], "teams_demo"
        )
        self.assertNotIn("ports", config["services"]["postgres"])
        for service in ("web", "worker"):
            values = config["services"][service]["environment"]
            self.assertEqual(values["DEEPSEEK_API_KEY"], "")
            self.assertEqual(values["LLM_ENABLE_LIVE_CALLS"], "false")
            self.assertEqual(values["STORAGE_DIR"], "/app/storage/content")
            self.assertTrue(values["DATABASE_URL"].endswith("/teams_demo"))
            self.assertEqual(
                config["services"][service]["image"], "teams-platform:demo"
            )
        self.assertEqual(config["services"]["web"]["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(config["services"]["web"]["ports"][0]["published"], "18005")
        self.assertNotIn("fixture-key-never-pass-through", result.stdout)
        self.assertNotIn("production", result.stdout)

    def test_invalid_ports_fail_before_docker_or_initialization(self):
        for value in ("0", "65536", "abc"):
            result = subprocess.run(
                ["python3", str(ROOT / "deploy/demo.py"), "--port", value],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("port", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
