"""Start the isolated fictional demo: python3 deploy/demo.py (Python 3.9+, Docker Compose)."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "teams-platform-demo"
VOLUMES = {
    name: f"{PROJECT}_{name}" for name in ("postgres_demo_data", "files_demo_data")
}


def demo_environment(inherited, port):
    # Keep Docker discovery/auth plumbing, never application/database/model variables.
    names = {
        "PATH",
        "HOME",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "SSH_AUTH_SOCK",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
    }
    return {
        **{key: value for key, value in inherited.items() if key in names},
        "DEMO_WEB_PORT": str(port),
    }


def compose_command():
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT / "deploy"),
        "--env-file",
        os.devnull,
        "--project-name",
        PROJECT,
        "-f",
        str(ROOT / "deploy/compose.demo.yaml"),
    ]


def _port(raw):
    try:
        value = int(raw)
        if not 1 <= value <= 65535:
            raise ValueError
        return value
    except ValueError:
        raise argparse.ArgumentTypeError(
            "port must be an integer from 1 to 65535"
        ) from None


def reset_volumes(environment):
    """Preflight only: never derive deletion targets from configurable Compose names."""
    config = json.loads(
        subprocess.run(
            compose_command() + ["--profile", "seed", "config", "--format", "json"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    volumes = config.get("volumes", {})
    if config.get("name") != PROJECT or set(volumes) != set(VOLUMES):
        raise ValueError("Unexpected demo project/volumes")
    for logical, fixed in VOLUMES.items():
        if volumes[logical].get("name") != fixed or volumes[logical].get("external"):
            raise ValueError("Only fixed demo-owned volumes may be reset")
    services = config.get("services", {})
    expected = {
        "postgres": "postgres_demo_data",
        "web": "files_demo_data",
        "worker": "files_demo_data",
        "seed": "files_demo_data",
    }
    if set(services) != set(expected):
        raise ValueError("Unexpected demo services")
    for service, source in expected.items():
        mounts = services[service].get("volumes", [])
        if (
            len(mounts) != 1
            or mounts[0].get("type") != "volume"
            or mounts[0].get("source") != source
        ):
            raise ValueError("Unexpected demo storage mount")
    existing = set(
        subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    targets = [name for name in VOLUMES.values() if name in existing]
    if targets:
        inspected = json.loads(
            subprocess.run(
                ["docker", "volume", "inspect", *targets],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        if {volume["Name"] for volume in inspected} != set(targets):
            raise ValueError("Unexpected volume inspection")
        for volume in inspected:
            labels = volume.get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != PROJECT
                or VOLUMES.get(labels.get("com.docker.compose.volume"))
                != volume["Name"]
            ):
                raise ValueError("Volume is not owned by the demo project")
    return targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=18005)
    parser.add_argument(
        "--no-build", action="store_true", help="Use the existing demo image"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stop", action="store_true", help="Stop only demo services; preserve volumes"
    )
    mode.add_argument(
        "--reset",
        action="store_true",
        help="Confirm deletion of the two fixed demo volumes and regenerate",
    )
    args = parser.parse_args()
    environment = demo_environment(os.environ, args.port)

    def run(*arguments):
        return subprocess.run(
            compose_command() + list(arguments), env=environment, check=True
        )

    # One lock across checkouts as all launchers target the same fixed Compose project.
    lock_path = Path(tempfile.gettempdir()) / "teams-platform-demo-launch.lock"
    reset_started = False
    try:
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run("config", "--quiet")
            if args.stop:
                run("stop")
                print("演示服务已停止，数据库和文件卷保留。", flush=True)
                return 0
            targets = []
            if args.reset:
                print(
                    "重置会永久删除以下两个固定演示卷并重新生成虚构剧情：\n"
                    + "\n".join(VOLUMES.values()),
                    flush=True,
                )
                try:
                    confirmed = input(f"输入 {PROJECT} 确认重置：") == PROJECT
                except EOFError:
                    confirmed = False
                if not confirmed:
                    print("已取消重置，演示卷保留。", file=sys.stderr)
                    return 1
                targets = reset_volumes(environment)
            if not args.no_build:
                run("build", "web")
            if args.reset:
                reset_started = True
                run(
                    "down"
                )  # No -v: deletion is restricted to the explicit fixed names below.
                if targets:
                    subprocess.run(
                        ["docker", "volume", "rm", *targets],
                        env=environment,
                        check=True,
                    )
            # No worker may race the initial fixed review tasks; seeding owns the transaction.
            run("stop", "web", "worker")
            run("up", "-d", "--wait", "--wait-timeout", "90", "postgres")
            run("run", "--rm", "--no-deps", "-T", "seed")
            run(
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "90",
                "web",
                "worker",
            )
            url = f"http://127.0.0.1:{args.port}"
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with direct.open(url + "/api/health", timeout=10) as response:
                if response.status != 200:
                    raise ValueError("Demo health failed")
            print(
                f"\n演示已就绪：{url}\n"
                "负责人 demo-leader；组员 demo-writer / demo-analyst\n"
                "共用演示密码：TeamsDemo-Only-2026\n"
                "虚构数据，预置审核使用固定响应，真实模型调用关闭。\n"
                "再次运行保留现场；停止用 python3 deploy/demo.py --stop；重新生成用 --reset。",
                flush=True,
            )
    except BlockingIOError:
        print("另一个演示启动/停止/重置命令正在运行。", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError, ValueError, urllib.error.URLError):
        state = (
            "重置未完成，演示卷可能已删除；请按deploy/DEMO.md检查并重新运行 --reset。"
            if reset_started
            else "仅演示服务可能已停止；请按deploy/DEMO.md检查，数据库和文件卷未删除。"
        )
        print("演示启动失败，不能当作已就绪。" + state, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "演示启动中断；"
            + ("重置可能已删除演示卷。" if reset_started else "原卷保留。"),
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
