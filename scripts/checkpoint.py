#!/usr/bin/env python3
"""Save and restore UTF-8 Markdown checkpoints; Python standard library only."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class CheckpointError(Exception):
    """An unsafe or invalid checkpoint operation."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(data):
    """Check readable Markdown, not factual accuracy or task completeness."""
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CheckpointError("Checkpoint must be UTF-8 text.") from exc
    if not content.strip() or "\x00" in content or "\ufffd" in content:
        raise CheckpointError("Checkpoint is empty or contains invalid text.")
    if not re.search(r"^#{1,6}\s+\S", content, re.MULTILINE):
        raise CheckpointError("Checkpoint needs at least one Markdown heading.")


def read_optional(path):
    if path.is_symlink():
        raise CheckpointError(f"Refusing a symbolic-link checkpoint: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def fingerprint(data):
    return "missing" if data is None else digest(data)


def inspect(path):
    data = read_optional(path)
    result = {"path": str(path), "exists": data is not None,
              "sha256": fingerprint(data), "valid_text": False}
    if data is not None:
        try:
            validate(data)
            result["valid_text"] = True
        except CheckpointError as exc:
            result["error"] = str(exc)
    return result


def code_state(root, paths):
    """Fingerprint explicit project files, including missing files, without code content."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise CheckpointError("--root must be a project directory.")
    files = []
    for relative in sorted(set(paths)):
        given = Path(relative)
        if given.is_absolute() or ".." in given.parts:
            raise CheckpointError("State paths must be relative files inside the project.")
        path = root / given
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise CheckpointError(f"Cannot fingerprint an external or symbolic-link file: {relative}")
        if path.is_dir():
            raise CheckpointError(f"Pass explicit files, not directories: {relative}")
        data = read_optional(path)
        files.append({"path": given.as_posix(), "sha256": fingerprint(data)})
    notes = []
    head = None
    status = None
    try:
        head_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, timeout=15, check=False)
        if head_result.returncode == 0:
            head = head_result.stdout.decode("ascii").strip()
        else:
            notes.append("HEAD unavailable (non-Git project or no commit yet).")
        status_result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), "status", "--porcelain=v1",
             "--untracked-files=all", "--", *[item["path"] for item in files]],
            capture_output=True, timeout=15, check=False)
        if status_result.returncode == 0:
            status = status_result.stdout.decode("utf-8", errors="backslashreplace")
        else:
            notes.append("Git status unavailable; file hashes still apply.")
    except (OSError, subprocess.TimeoutExpired):
        notes.append("Git unavailable or timed out; file hashes still apply.")
    snapshot = {"head": head, "git_status": status, "files": files}
    identity = digest(json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode("utf-8"))
    return {"state_id": identity, **snapshot, "notes": notes}


@contextmanager
def writer_lock(target):
    """OS advisory lock, released even when the process exits unexpectedly."""
    lock_path = target.with_name(target.name + ".lock")
    if lock_path.is_symlink():
        raise CheckpointError(f"Refusing a symbolic-link lock: {lock_path}")
    # Keep the lock file after closing: unlinking it could split the lock domain.
    with open(lock_path, "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CheckpointError("Another checkpoint writer holds the lock.") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CheckpointError("Another checkpoint writer holds the lock.") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def staged_file(target, data):
    """Write and verify in the destination directory before any replacement."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.mem-fix-", suffix=".tmp", dir=target.parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        written = path.read_bytes()
        validate(written)
        if written != data:
            raise CheckpointError("Temporary checkpoint failed byte verification.")
        yield path
    finally:
        path.unlink(missing_ok=True)


def require_expected(target, expected):
    current = read_optional(target)
    actual = fingerprint(current)
    if actual != expected:
        raise CheckpointError(
            f"Checkpoint changed; expected {expected}, found {actual}. "
            "Re-read and merge before saving.")
    return current


def commit(target, expected, source=None, restore=False):
    if expected != "missing" and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CheckpointError("--expect must be an inspected SHA-256 or 'missing'.")
    if source is not None and source.resolve() == target.resolve():
        raise CheckpointError("Use a separate draft; do not edit the target first.")
    previous = target.with_name(target.name + ".prev")
    if previous.is_symlink():
        raise CheckpointError(f"Refusing a symbolic-link backup: {previous}")
    # Read/validate a draft before creating directories or taking the lock.
    draft = None if restore else source.read_bytes()
    if draft is not None:
        validate(draft)
    target.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(target):
        old = require_expected(target, expected)
        if restore:
            candidate = read_optional(previous)
            if candidate is None:
                raise CheckpointError("No previous checkpoint is available.")
        else:
            candidate = draft
            if old is not None:
                try:
                    validate(old)
                except CheckpointError as exc:
                    raise CheckpointError(
                        "Current checkpoint is damaged; inspect and restore the "
                        "previous snapshot before saving a new checkpoint.") from exc
        validate(candidate)
        if candidate == old:
            return {"path": str(target), "sha256": digest(candidate),
                    "changed": False, "operation": "restore" if restore else "save"}
        with staged_file(target, candidate) as ready:
            if old is not None and not restore:
                with staged_file(previous, old) as backup_ready:
                    os.replace(backup_ready, previous)
            # Detect non-cooperating changes observed since taking the lock.
            require_expected(target, expected)
            os.replace(ready, target)
        if read_optional(target) != candidate:
            raise CheckpointError("Post-save verification failed; inspect the target.")
        return {"path": str(target), "sha256": digest(candidate), "changed": True,
                "previous": str(previous) if previous.exists() else None,
                "operation": "restore" if restore else "save"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect", help="Read current hash and text validity.")
    read.add_argument("target", type=Path)
    state = commands.add_parser("state", help="Hash explicit code files and report Git state.")
    state.add_argument("--root", type=Path, required=True)
    state.add_argument("--path", action="append", required=True, dest="paths")
    save = commands.add_parser("save", help="Save a separate draft with conflict detection.")
    save.add_argument("target", type=Path)
    save.add_argument("--source", type=Path, required=True)
    save.add_argument("--expect", required=True)
    restore = commands.add_parser("restore", help="Restore .prev without rotating it.")
    restore.add_argument("target", type=Path)
    restore.add_argument("--expect", required=True)
    args = parser.parse_args()
    try:
        if args.command == "state":
            result = code_state(args.root, args.paths)
        elif args.command == "inspect":
            result = inspect(args.target.absolute())
        else:
            result = commit(args.target.absolute(), args.expect, getattr(args, "source", None),
                            restore=args.command == "restore")
        print(json.dumps(result, ensure_ascii=True))
    except (CheckpointError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
