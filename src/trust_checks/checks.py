from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

from scripts.trust import gitfiles
from scripts.trust.gitfiles import Change
from scripts.trust.scan import (
    BAD_DATE,
    MISSING,
    Comment,
    FileScan,
    Suppression,
    is_target,
    language_of,
    scan_texts,
)

EXPIRY_DAYS = 90
KST = timedelta(hours=9)
APPROVED_ENV = "TRUST_BASELINE_APPROVED"
TODAY_ENV = "TRUST_TODAY"
LABEL = "baseline-approved"


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Candidate:
    path: str
    text: str | None
    base_text: str | None


def today(env: Mapping[str, str] = os.environ) -> date:
    value = env.get(TODAY_ENV)
    if value:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{TODAY_ENV}={value} — YYYY-MM-DD 로 쓴다")
        return date.fromisoformat(value)
    return (datetime.now(UTC) + KST).date()


def approved(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(APPROVED_ENV) == "1"


def _limit(now: date) -> date:
    return now + timedelta(days=EXPIRY_DAYS)


def disallowed_message() -> str:
    return (
        "허용 안 된 주석 — 지우고, 꼭 남길 내용이면 함수·변수 이름이나 시험으로 옮기거나 "
        "docs/engineering-notes.md 에 적는다. 바깥 제약이면 '외부: ', 이슈면 이슈 주소로 시작한다"
    )


def malformed_reason_message(now: date) -> str:
    return (
        "'검사 끄기:' 설명에 이유·만료 형식이 없다 — "
        f"'검사 끄기: <이유> · 만료 YYYY-MM-DD' 로 쓴다(만료는 오늘부터 {EXPIRY_DAYS}일 이내, {_limit(now).isoformat()} 까지)"
    )


def orphan_message() -> str:
    return "'검사 끄기:' 설명 바로 아래 줄에 검사 끄기 표시가 없다 — 표시 바로 윗줄로 옮기거나 지운다"


def missing_message(suppression: Suppression, now: date) -> str:
    hint = f"'<이유> · 만료 {_limit(now).isoformat()}'"
    if suppression.marker == "eslint-disable":
        hint = f"'-- <이유> · 만료 {_limit(now).isoformat()}'"
    return (
        f"검사 끄기 표시 '{suppression.key}' 에 이유·만료가 없다 — {hint} 를 붙인다(오늘부터 {EXPIRY_DAYS}일 이내). "
        "도구가 같은 줄 덧붙임을 받지 못하면 바로 윗줄에 '검사 끄기: <이유> · 만료 YYYY-MM-DD' 주석을 둔다"
    )


def bad_date_message(suppression: Suppression) -> str:
    return f"검사 끄기 표시 '{suppression.key}' 의 만료 날짜가 틀렸다 — 실제 날짜를 YYYY-MM-DD 로 쓴다"


def expired_message(suppression: Suppression) -> str:
    expiry = suppression.expiry.isoformat() if suppression.expiry else "?"
    return (
        f"검사 끄기 표시 '{suppression.key}' 의 만료({expiry})가 지났다 — 원인을 고쳐 표시를 없앤다. "
        f"더 미뤄야 하면 이유를 새로 적고 만료를 미룬 뒤 PR 에 사람이 {LABEL} 표지를 단다"
    )


def too_far_message(suppression: Suppression, now: date) -> str:
    expiry = suppression.expiry.isoformat() if suppression.expiry else "?"
    return (
        f"새로 넣거나 바꾼 검사 끄기 표시 '{suppression.key}' 의 만료({expiry})가 "
        f"오늘부터 {EXPIRY_DAYS}일({_limit(now).isoformat()})을 넘는다 — {_limit(now).isoformat()} 이전 날짜로 쓴다"
    )


def extended_message(suppression: Suppression, previous: Suppression) -> str:
    before = previous.expiry.isoformat() if previous.expiry else "?"
    after = suppression.expiry.isoformat() if suppression.expiry else "?"
    return (
        f"검사 끄기 표시 '{suppression.key}' 의 만료를 미뤘다({before} → {after}) — "
        f"PR 에 사람이 {LABEL} 표지를 달아야 통과한다. 에이전트는 이 표지를 달지 않는다"
    )


def same_reason_message(suppression: Suppression) -> str:
    return f"검사 끄기 표시 '{suppression.key}' 의 만료를 미뤘는데 이유가 그대로다 — 미루는 이유를 새로 적는다"


def pair_suppressions(current: Sequence[Suppression], base: Sequence[Suppression]) -> dict[int, Suppression]:
    candidates = sorted(
        (abs(now.line - before.line), now.line, before.line, now_index, before_index)
        for now_index, now in enumerate(current)
        for before_index, before in enumerate(base)
        if now.key == before.key
    )
    paired: dict[int, Suppression] = {}
    used: set[int] = set()
    for _, _, _, now_index, before_index in candidates:
        if now_index in paired or before_index in used:
            continue
        paired[now_index] = base[before_index]
        used.add(before_index)
    return paired


def _comment_message(comment: Comment, now: date) -> str:
    if comment.orphan:
        return orphan_message()
    if comment.malformed:
        return malformed_reason_message(now)
    return disallowed_message()


def judge_scan(scan: FileScan, base: FileScan | None, now: date, is_approved: bool) -> list[Problem]:
    path = scan.path
    if scan.error is not None:
        return [Problem(path, scan.error.line, f"파일을 읽지 못했다({scan.error.message}) — 문법 오류를 고친다")]
    problems = [Problem(path, comment.line, _comment_message(comment, now)) for comment in scan.disallowed]
    previous = pair_suppressions(scan.suppressions, base.suppressions if base and base.error is None else ())
    for index, suppression in enumerate(scan.suppressions):
        problems.extend(_judge_suppression(path, suppression, previous.get(index), now, is_approved))
    return sorted(problems, key=lambda problem: problem.line)


def _judge_suppression(
    path: str, suppression: Suppression, previous: Suppression | None, now: date, is_approved: bool
) -> list[Problem]:
    line = suppression.line
    if suppression.problem == MISSING:
        return [Problem(path, line, missing_message(suppression, now))]
    if suppression.problem == BAD_DATE or suppression.expiry is None:
        return [Problem(path, line, bad_date_message(suppression))]
    if suppression.expiry < now:
        return [Problem(path, line, expired_message(suppression))]
    changed = previous is None or (previous.reason, previous.expiry) != (suppression.reason, suppression.expiry)
    problems: list[Problem] = []
    if changed and suppression.expiry > _limit(now):
        problems.append(Problem(path, line, too_far_message(suppression, now)))
    if previous is not None and previous.expiry is not None and suppression.expiry > previous.expiry:
        if previous.reason == suppression.reason:
            problems.append(Problem(path, line, same_reason_message(suppression)))
        if not is_approved:
            problems.append(Problem(path, line, extended_message(suppression, previous)))
    return problems


def _decode(path: str, data: bytes | None) -> tuple[str | None, Problem | None]:
    if data is None:
        return None, None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, Problem(path, 1, "UTF-8 로 읽지 못했다 — 파일을 UTF-8 로 저장한다")


def judge_candidates(candidates: Sequence[Candidate], now: date, is_approved: bool) -> list[Problem]:
    current = scan_texts([(item.path, item.text or "") for item in candidates])
    base = scan_texts([(item.path, item.base_text) for item in candidates if item.base_text is not None])
    base_iter = iter(base)
    problems: list[Problem] = []
    for item, scan in zip(candidates, current, strict=True):
        previous = next(base_iter) if item.base_text is not None else None
        problems.extend(judge_scan(scan, previous, now, is_approved))
    return problems


Reader = Callable[[str], bytes | None]


def _collect(
    root: Path, changes: Sequence[Change], read_current: Reader, base_ref: str | None
) -> tuple[list[Candidate], list[Problem]]:
    candidates: list[Candidate] = []
    problems: list[Problem] = []
    for change in changes:
        if not is_target(change.path):
            continue
        data = read_current(change.path)
        if data is None:
            continue
        text, problem = _decode(change.path, data)
        if problem is not None:
            problems.append(problem)
            continue
        base_text = None
        if base_ref is not None and change.base_path is not None:
            base_text, _ = _decode(change.path, gitfiles.show(root, f"{base_ref}:{change.base_path}"))
        candidates.append(Candidate(change.path, text, base_text))
    return candidates, problems


def _read_worktree(root: Path) -> Reader:
    def read(path: str) -> bytes | None:
        file = root / path
        if file.is_symlink() or not file.is_file():
            return None
        return file.read_bytes()

    return read


def _read_index(root: Path) -> Reader:
    def read(path: str) -> bytes | None:
        return gitfiles.show(root, f":{path}")

    return read


def check_comments_since(root: Path, ref: str, now: date, is_approved: bool) -> tuple[int, list[Problem]]:
    base, changes = gitfiles.changes_since(root, ref)
    candidates, problems = _collect(root, changes, _read_worktree(root), base)
    problems.extend(judge_candidates(candidates, now, is_approved))
    return len(candidates), problems


def check_comments_staged(root: Path, now: date, is_approved: bool) -> tuple[int, list[Problem]]:
    base, changes = gitfiles.staged_changes(root)
    candidates, problems = _collect(root, changes, _read_index(root), base)
    problems.extend(judge_candidates(candidates, now, is_approved))
    return len(candidates), problems


def tracked_targets(root: Path) -> list[str]:
    return [path for path in gitfiles.tracked_files(root) if is_target(path)]


def scan_repo(root: Path) -> tuple[list[FileScan], list[Problem]]:
    items: list[tuple[str, str]] = []
    problems: list[Problem] = []
    read = _read_worktree(root)
    for path in tracked_targets(root):
        text, problem = _decode(path, read(path))
        if problem is not None:
            problems.append(problem)
        elif text is not None:
            items.append((path, text))
    return scan_texts(items), problems


class CheckError(Exception):
    def __init__(self, message: str, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details)


def find_expired(root: Path, now: date) -> list[Problem]:
    scans, unreadable = scan_repo(root)
    unreadable = list(unreadable)
    problems: list[Problem] = []
    for scan in scans:
        if scan.error is not None:
            unreadable.append(
                Problem(scan.path, scan.error.line, f"파일을 읽지 못했다({scan.error.message}) — 문법 오류를 고친다")
            )
            continue
        problems.extend(
            Problem(scan.path, suppression.line, expired_message(suppression))
            for suppression in scan.suppressions
            if suppression.expiry is not None and suppression.expiry < now
        )
    if unreadable:
        raise CheckError(
            f"파일 {len(unreadable)}개를 읽지 못해 만료 목록을 만들 수 없다 — 아래 파일을 고친다",
            [problem.render() for problem in sorted(unreadable, key=lambda problem: (problem.path, problem.line))],
        )
    return sorted(problems, key=lambda problem: (problem.path, problem.line))


BACKEND_TOOL_CONFIGS = frozenset(
    {"ruff.toml", ".ruff.toml", "ty.toml", ".importlinter", "setup.cfg", "tox.ini", ".flake8"}
)
BACKEND_PYPROJECT = "backend/pyproject.toml"
IGNORE_FILE_NAMES = frozenset({".ignore", ".rgignore"})
TOOL_TABLES = ("ruff", "ty", "importlinter")


def _tool_config_message() -> str:
    return (
        "검사 도구 설정 파일이 따로 있다 — 지운다. ruff·ty·import-linter 설정은 backend/pyproject.toml 한 곳에 둔다"
        "(다른 설정 파일이 있으면 도구가 pyproject 설정을 무시한다)"
    )


def _nested_tool_tables(root: Path, path: str) -> bool:
    file = root / path
    if not file.is_file():
        return False
    text = file.read_text(encoding="utf-8", errors="replace")
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        return any(f"[tool.{name}" in text for name in TOOL_TABLES)
    return isinstance(tool, dict) and any(name in tool for name in TOOL_TABLES)


def check_repo(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    for path in gitfiles.tracked_files(root):
        pure = PurePosixPath(path)
        if pure.name == "CLAUDE.md":
            problems.append(
                Problem(path, 1, "추적 파일 CLAUDE.md 가 있다 — 지우고 내용은 같은 폴더의 AGENTS.md 로 옮긴다")
            )
        if any(".disabled" in part for part in pure.parts):
            problems.append(
                Problem(
                    path,
                    1,
                    "이름에 .disabled 가 든 추적 파일이 있다 — 끈 파일을 남기지 말고 지우거나 고쳐서 다시 켠다. "
                    "시험을 잠시 빼야 하면 ci_quarantine 표시를 쓴다",
                )
            )
        if pure.name in IGNORE_FILE_NAMES:
            problems.append(
                Problem(
                    path,
                    1,
                    "검사 도구가 따르는 무시 목록 파일이 있다 — 지운다. 여기 적힌 파일은 ruff·ty 같은 검사 도구가 건너뛴다. "
                    "검사에서 뺄 파일은 backend/pyproject.toml 의 exclude 로만 정한다",
                )
            )
        if pure.parts[:1] != ("backend",):
            continue
        if pure.name in BACKEND_TOOL_CONFIGS:
            problems.append(Problem(path, 1, _tool_config_message()))
        elif pure.name == "pyproject.toml" and path != BACKEND_PYPROJECT and _nested_tool_tables(root, path):
            problems.append(Problem(path, 1, _tool_config_message()))
    problems.extend(
        Problem(path, 1, "추적 파일을 무시 목록에 넣으면 검사 도구가 건너뛴다 — 무시 목록에서 뺀다(.gitignore 등)")
        for path in gitfiles.ignored_tracked_files(root)
        if language_of(path) is not None
    )
    return problems
