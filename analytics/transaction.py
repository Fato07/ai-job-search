from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.write-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f".{path.name}.backup-",
        delete=False,
    ) as handle:
        backup = Path(handle.name)
    try:
        shutil.copyfile(path, backup)
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
        return backup
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def _restore(entry: Mapping[str, object]) -> None:
    destination = Path(str(entry["destination"]))
    if not bool(entry["existed"]):
        destination.unlink(missing_ok=True)
        _fsync_directory(destination.parent)
        return
    backup = Path(str(entry["backup"]))
    if not backup.is_file():
        raise RuntimeError(f"transaction backup missing for {destination}")
    restore: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.restore-",
            delete=False,
        ) as handle:
            restore = Path(handle.name)
        shutil.copyfile(backup, restore)
        with restore.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(restore, destination)
        _fsync_directory(destination.parent)
    finally:
        if restore is not None:
            restore.unlink(missing_ok=True)


def _cleanup(journal_path: Path, entries: Iterable[Mapping[str, object]]) -> None:
    journal_path.unlink(missing_ok=True)
    _fsync_directory(journal_path.parent)
    for entry in entries:
        for key in ("staged", "backup"):
            value = entry.get(key)
            if value:
                Path(str(value)).unlink(missing_ok=True)


def _load_journal(journal_path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot recover transaction: {journal_path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError(f"invalid transaction journal: {journal_path}")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"invalid transaction journal: {journal_path}")
    if any(not isinstance(entry, dict) for entry in entries):
        raise RuntimeError(f"invalid transaction journal: {journal_path}")
    return entries


def recover_transaction(journal_path: Path, destinations: Iterable[Path]) -> None:
    if not journal_path.exists():
        return
    entries = _load_journal(journal_path)
    expected = {str(path.resolve()) for path in destinations}
    recorded = {str(entry.get("destination")) for entry in entries}
    if recorded != expected or len(recorded) != len(entries):
        raise RuntimeError(f"transaction journal destinations differ: {journal_path}")
    for entry in entries:
        _restore(entry)
    _cleanup(journal_path, entries)


def commit_staged_files(journal_path: Path, staged_files: Mapping[Path, Path]) -> None:
    if not staged_files:
        raise ValueError("transaction requires at least one staged file")
    destinations = list(staged_files)
    if len(set(destinations)) != len(destinations):
        raise ValueError("transaction destinations must be unique")
    if journal_path.exists():
        raise RuntimeError(f"unrecovered transaction journal exists: {journal_path}")

    entries: list[dict[str, object]] = []
    journal_written = False
    try:
        for destination, staged in staged_files.items():
            if not staged.is_file():
                raise ValueError(f"staged file is missing: {staged}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup = _backup_file(destination)
            entries.append(
                {
                    "destination": str(destination.resolve()),
                    "staged": str(staged.resolve()),
                    "backup": str(backup.resolve()) if backup else "",
                    "existed": destination.exists(),
                }
            )
        _write_journal(journal_path, {"version": 1, "entries": entries})
        journal_written = True
        try:
            for entry in entries:
                destination = Path(str(entry["destination"]))
                os.replace(Path(str(entry["staged"])), destination)
                _fsync_directory(destination.parent)
        except Exception:
            for entry in entries:
                _restore(entry)
            _cleanup(journal_path, entries)
            raise
        _cleanup(journal_path, entries)
    except BaseException:
        if not journal_written:
            for entry in entries:
                backup = entry.get("backup")
                if backup:
                    Path(str(backup)).unlink(missing_ok=True)
        raise
