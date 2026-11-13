"""Start the isolated fictional demo: python3 deploy/demo.py (Python 3.9+, Docker Compose)."""

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        "teams-platform-demo",
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
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=18005)
    parser.add_argument("--no-build", action="store_true", help="Use the existing demo image")
    parser.add_argument(
        "--stop", action="store_true", help="Stop only demo services; preserve volumes"
    )
    args = parser.parse_args()
    environment = demo_environment(os.environ, args.port)

    def run(*arguments):
        return subprocess.run(compose_command() + list(arguments), env=environment, check=True)

    # One lock across checkouts as all launchers target the same fixed Compose project.
    lock_path = Path(tempfile.gettempdir()) / "teams-platform-demo-launch.lock"
    try:
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run("config", "--quiet")
            if args.stop:
                run("stop")
                print("演示服务已停止，数据库和文件卷保留。", flush=True)
                return 0
            if not args.no_build:
                run("build", "web")
            # No worker may race the initial fixed review tasks; seeding owns the transaction.
            run("stop", "web", "worker")
            run("up", "-d", "--wait", "--wait-timeout", "90", "postgres")
            run("run", "--rm", "--no-deps", "-T", "seed")
            run("up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "web", "worker")
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
                "再次运行保留现场；停止用 python3 deploy/demo.py --stop。",
                flush=True,
            )
    except BlockingIOError:
        print("另一个演示启动/停止命令正在运行。", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError, ValueError, urllib.error.URLError):
        print(
            "演示启动失败，不能当作已就绪。仅演示服务可能已停止；请按deploy/DEMO.md检查，"
            "数据库和文件卷未删除。",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("演示启动中断；请检查演示服务状态，原卷保留。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
