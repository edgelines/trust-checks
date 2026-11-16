from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    path: str
    base_path: str | None


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} 실패: {message}")
    return result.stdout


def _split(output: bytes) -> list[str]:
    return [part.decode("utf-8", "surrogateescape") for part in output.split(b"\0") if part]


def tracked_files(root: Path) -> list[str]:
    return _split(git(root, "ls-files", "-z"))


def ignored_tracked_files(root: Path) -> list[str]:
    return _split(git(root, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard"))


def untracked_files(root: Path) -> list[str]:
    return _split(git(root, "ls-files", "--others", "--exclude-standard", "-z"))


def merge_base(root: Path, ref: str) -> str:
    return git(root, "merge-base", ref, "HEAD").decode().strip()


def has_head(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"], capture_output=True, check=False
    )
    return result.returncode == 0


def _parse_name_status(output: bytes) -> list[Change]:
    parts = _split(output)
    changes: list[Change] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        if status[:1] in ("R", "C"):
            changes.append(Change(parts[index + 2], parts[index + 1]))
            index += 3
            continue
        path = parts[index + 1]
        index += 2
        if status[:1] == "D":
            continue
        changes.append(Change(path, None if status[:1] == "A" else path))
    return changes


def changes_since(root: Path, ref: str) -> tuple[str, list[Change]]:
    base = merge_base(root, ref)
    output = git(root, "diff", "--no-ext-diff", "--name-status", "-z", "-M", base)
    changes = _parse_name_status(output)
    known = {change.path for change in changes}
    changes.extend(Change(path, None) for path in untracked_files(root) if path not in known)
    return base, changes


def staged_changes(root: Path) -> tuple[str | None, list[Change]]:
    if not has_head(root):
        return None, [Change(path, None) for path in _split(git(root, "ls-files", "--cached", "-z"))]
    output = git(root, "diff", "--cached", "--no-ext-diff", "--name-status", "-z", "-M", "HEAD")
    return "HEAD", _parse_name_status(output)


def show(root: Path, spec: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(root), "show", spec], capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else None
