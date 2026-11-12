"""Consistent stopped-writer backups and empty-target restores; Python 3.12 + Compose."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PAYLOADS = ("database.dump", "storage.tar")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def seal_backup(directory, metadata):
    manifest = {
        "format": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "metadata": metadata,
        "sha256": {name: digest(directory / name) for name in PAYLOADS},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def verify_backup(directory):
    import tarfile

    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["format"] != 1 or set(manifest["sha256"]) != set(PAYLOADS):
            raise ValueError("Unknown or incomplete backup manifest")
        for name in PAYLOADS:
            path = directory / name
            if path.is_symlink() or not path.is_file() or digest(path) != manifest["sha256"][name]:
                raise ValueError(f"Missing or corrupt backup payload: {name}")
        with tarfile.open(directory / "storage.tar") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Storage archive contains an unsafe path or link")
        return manifest
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as error:
        raise ValueError("Incomplete or unreadable backup") from error


class Compose:
    def __init__(self, env_file, project_name, overrides):
        if not env_file or not env_file.is_file():
            raise ValueError("An explicit, existing --env-file is required")
        self.env_file = env_file.resolve()
        self.command = [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT / "deploy"),
            "--env-file",
            str(self.env_file),
            "--project-name",
            project_name,
            "-f",
            str(ROOT / "deploy/compose.production.yaml"),
        ]
        for override in overrides:
            self.command += ["-f", str(override.resolve())]
        self.run("config", "--quiet")

    def run(self, *arguments, **options):
        return subprocess.run(self.command + list(arguments), check=True, **options)

    def running(self):
        return set(
            self.run(
                "ps",
                "--services",
                "--filter",
                "status=running",
                capture_output=True,
                text=True,
            ).stdout.split()
        )

    @contextmanager
    def locked(self):
        # Serialise backup/restore by operators using this production environment file.
        lock = self.env_file.with_name(self.env_file.name + ".maintenance.lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def backup(compose, destination):
    if destination.exists():
        raise ValueError("Backup destination already exists; choose a new directory")
    if not {"postgres", "web", "worker"} <= compose.running():
        raise ValueError("Backup requires running postgres, web and worker services")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.partial-", dir=destination.parent))
    try:
        try:
            compose.run("stop", "web", "worker")
            with (staging / "database.dump").open("wb") as target:
                compose.run(
                    "exec",
                    "-T",
                    "postgres",
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                    stdout=target,
                )
            with (staging / "storage.tar").open("wb") as target:
                compose.run(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--entrypoint",
                    "tar",
                    "worker",
                    "-C",
                    "/app/storage",
                    "-cf",
                    "-",
                    ".",
                    stdout=target,
                )
            image = compose.run(
                "images", "-q", "web", capture_output=True, text=True
            ).stdout.strip()
            seal_backup(staging, {"application_image": image})
            verify_backup(staging)
            staging.rename(destination)
        finally:
            # Resume even when dump/archive/verification fails; never label a partial as complete.
            compose.run("start", "--wait", "web", "worker")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore(compose, source):
    verify_backup(source)
    if {"web", "worker"} & compose.running():
        raise ValueError("Restore requires stopped web and worker services")
    compose.run("up", "-d", "--wait", "postgres")
    count = compose.run(
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc '
        '"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'\"",
        capture_output=True,
        text=True,
    ).stdout.strip()
    if count != "0":
        raise ValueError("Restore target database is not empty; no data was overwritten")
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "worker",
        "-c",
        "from pathlib import Path; import sys; "
        "sys.exit('Target storage is not empty' if any(Path('/app/storage').iterdir()) else 0)",
    )
    with (source / "storage.tar").open("rb") as payload:
        compose.run(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "tar",
            "worker",
            "-C",
            "/app/storage",
            "-xf",
            "-",
            "--no-same-owner",
            stdin=payload,
        )
    with (source / "database.dump").open("rb") as payload:
        compose.run(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            "--exit-on-error --single-transaction --no-owner --no-privileges",
            stdin=payload,
        )
    # Stay stopped: the operator must inspect and then start the pinned application image.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-name", default="teams-platform-production")
    parser.add_argument("--override", type=Path, action="append", default=[])
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.action == "verify":
            verify_backup(args.directory)
        else:
            compose = Compose(args.env_file, args.project_name, args.override)
            with compose.locked():
                if args.action == "backup":
                    backup(compose, args.directory)
                else:
                    restore(compose, args.directory)
        print(f"{args.action}: complete ({args.directory})")
        if args.action == "restore":
            print(
                "Web/worker remain stopped. Inspect the restored data, then start the deployment."
            )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{args.action}: failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. Check service status before resuming operations.\n")


if __name__ == "__main__":
    main()
