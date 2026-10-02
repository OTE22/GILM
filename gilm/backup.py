"""SQLite online snapshots and verified restore into a new directory only."""

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

DATABASES = ("state.sqlite", "reporting.sqlite")


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")


def backup(source: Path, destination: Path):
    if destination.exists():
        raise ValueError("Backup destination must not already exist")
    if not all((source / name).is_file() for name in DATABASES):
        raise ValueError("Both application databases must exist")
    destination.mkdir(parents=True)
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Backup deadline exceeded")

    for name in DATABASES:
        with (
            sqlite3.connect((source / name).resolve().as_uri() + "?mode=ro", uri=True) as original,
            sqlite3.connect(destination / name) as snapshot,
        ):
            original.backup(snapshot, pages=128, progress=progress, sleep=0.05)
        check(destination / name)
    manifest = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {name: checksum(destination / name) for name in DATABASES},
        "consistency": "Each database is transactionally consistent; the pair is not an atomic cross-database snapshot.",
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def restore(source: Path, destination: Path):
    if destination.exists():
        raise ValueError("Restore destination must be new; existing data is never overwritten")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or set(manifest.get("files", {})) != set(DATABASES):
        raise ValueError("Invalid backup manifest")
    for name in DATABASES:
        if checksum(source / name) != manifest["files"][name]:
            raise ValueError("Backup checksum mismatch")
        check(source / name)
    destination.mkdir(parents=True)
    for name in DATABASES:
        shutil.copyfile(source / name, destination / name)
        check(destination / name)
    return {"restored": list(DATABASES), "destination": str(destination), "integrity": "passed"}
