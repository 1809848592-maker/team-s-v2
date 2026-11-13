"""Apply dev trial settings and an optional Mac LAN forwarder (Python 3.9+, Colima)."""

import fcntl
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".tools/dev-lan"
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def trial_environment(inherited):
    # Compose reads the key from the explicit platform/.env, never a terminal override.
    environment = {
        key: value for key, value in inherited.items() if key != "DEEPSEEK_API_KEY"
    }
    environment["DEV_LAN_ENDPOINT"] = ""
    return environment


def compose_command():
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / ".env"),
        "-p",
        "teams-platform-dev",
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(ROOT / "docker-compose.trial.yml"),
    ]


def lan_settings(config):
    if config.get("name") != "teams-platform-dev":
        raise ValueError("Unexpected Compose project")
    lan = config.get("x-dev-lan", {})
    enabled = str(lan.get("enabled", "false")).lower()
    if enabled not in {"true", "false"}:
        raise ValueError("DEV_LAN_ENABLED must be true or false")
    if enabled == "false":
        return None
    address = ipaddress.ip_address(lan.get("ip", ""))
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in net for net in PRIVATE_NETWORKS
    ):
        raise ValueError("Specify this Mac's private hotspot IPv4 address")
    profile = lan.get("colima_profile", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
        raise ValueError("Invalid Colima profile")
    ports = config.get("services", {}).get("web", {}).get("ports", [])
    if (
        len(ports) != 1
        or ports[0].get("host_ip") != "127.0.0.1"
        or ports[0].get("target") != 8000
    ):
        raise ValueError("The Docker web port must stay on loopback")
    port = int(ports[0]["published"])
    if not 1024 <= port <= 65535:
        raise ValueError("Use an unprivileged web port")
    # An address from a different network must fail before changing any services.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((str(address), 0))
    return {"ip": str(address), "port": port, "profile": profile}


def stop_forwarder(state, environment):
    control = state / "control.sock"
    record = state / "state.json"
    if control.exists():
        saved = json.loads(record.read_text())
        subprocess.run(
            [
                "ssh",
                "-F",
                str(state / "ssh.conf"),
                "-S",
                str(control),
                "-O",
                "exit",
                saved["host"],
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        for _ in range(30):
            if not control.exists():
                break
            time.sleep(0.1)
        if control.exists():
            raise ValueError("LAN control socket did not close")
    record.unlink(missing_ok=True)


def colima_connection(settings, environment):
    profile = settings["profile"]
    # The SSH target must be the same Docker daemon as the dev Compose project.
    local = subprocess.run(
        ["docker", "info", "--format", "{{.ID}}"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    guest = subprocess.run(
        ["colima", "-p", profile, "ssh", "--", "docker", "info", "--format", "{{.ID}}"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not local or local != guest:
        raise ValueError("Colima profile and Docker daemon differ")
    config = subprocess.run(
        ["colima", "ssh-config", profile],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    host = f"colima-{profile}"
    if not re.search(rf"^Host {re.escape(host)}$", config, re.MULTILINE):
        raise ValueError("Unexpected Colima SSH configuration")
    return config, host


def start_forwarder(state, settings, environment, connection):
    config, host = connection
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    conf = state / "ssh.conf"
    conf.write_text(config)
    conf.chmod(0o600)
    (state / "state.json").write_text(json.dumps({**settings, "host": host}))
    subprocess.run(
        [
            "ssh",
            "-F",
            str(conf),
            "-S",
            str(state / "control.sock"),
            "-o",
            "ControlMaster=yes",
            "-o",
            "ControlPersist=no",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-fNT",
            "-L",
            f"{settings['ip']}:{settings['port']}:127.0.0.1:{settings['port']}",
            host,
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def apply(inherited, state=STATE):
    environment = trial_environment(inherited)
    # Closing our Mac listener must work even if Compose configuration is broken.
    stop_forwarder(state, environment)
    config = json.loads(
        subprocess.run(
            compose_command() + ["config", "--format", "json"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    settings = lan_settings(config)
    # Prove the target matches before changing any Compose service.
    connection = colima_connection(settings, environment) if settings else None
    if settings:
        environment["DEV_LAN_ENDPOINT"] = f"{settings['ip']}:{settings['port']}"
    try:
        subprocess.run(
            compose_command()
            + [
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "web",
                "worker",
            ],
            env=environment,
            check=True,
        )
        if settings:
            start_forwarder(state, settings, environment, connection)
    except BaseException:
        stop_forwarder(state, environment)
        raise
    return settings


def main():
    try:
        lock = Path(tempfile.gettempdir()) / "teams-platform-dev-lan.lock"
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            settings = apply(os.environ)
        if settings:
            print(
                f"局域网已开启：http://{settings['ip']}:{settings['port']}；MCP地址追加 /mcp。\n"
                "仅限自己的热点、HTTP 明文、用完关闭。"
            )
        else:
            print("局域网已关闭；dev继续通过本机回环地址访问。")
        return 0
    except BlockingIOError:
        print("另一个dev局域网配置命令正在运行。", file=sys.stderr)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print(
            "dev局域网配置失败，不能当作已开放；检查IP、Colima profile、端口和Docker状态。\n"
            "原有监听可能已关闭，请用 DEV_LAN_ENABLED=false 再执行关闭。",
            file=sys.stderr,
        )
    except KeyboardInterrupt:
        print("配置中断；请执行关闭命令确认局域网监听已停止。", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
