from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from trust_checks import lexers
from trust_checks.lexers import LexError, RawComment

TS_HELPER = Path(__file__).resolve().with_name("ts_comments.mjs")

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
    ".rs": "rust",
    ".sh": "shell",
    ".bash": "shell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
}

EXTERNAL = "external"
LICENSE = "license"
ISSUE = "issue"
SUPPRESSION = "suppression"
SUPPRESSION_REASON = "suppression_reason"
DISALLOWED = "disallowed"
ALLOWED_CATEGORIES = frozenset({EXTERNAL, LICENSE, ISSUE, SUPPRESSION, SUPPRESSION_REASON})

MISSING = "missing"
BAD_DATE = "bad-date"

_MARKERS = (
    ("noqa", r"(?:(?:ruff|flake8)[ \t]*:[ \t]*)?noqa(?:[ \t]*:[ \t]*[A-Z]+[0-9]+(?:[ \t,]+[A-Z]+[0-9]+)*)?"),
    ("type: ignore", r"type:[ \t]*ignore(?:\[[^\]\n]*\])?"),
    ("ty: ignore", r"ty:[ \t]*ignore(?:\[[^\]\n]*\])?"),
    ("pyright: ignore", r"pyright:[ \t]*ignore(?:\[[^\]\n]*\])?"),
    ("pragma: no cover", r"pragma:[ \t]*no[ \t]+cover"),
    ("fmt", r"fmt:[ \t]*(?:off|on|skip)"),
    ("eslint-disable", r"eslint-(?:disable|enable)(?:-next-line|-line)?(?:[ \t]+(?!--)(?:(?![ \t]--).)+)?"),
    ("@ts-ignore", r"@ts-ignore"),
    ("@ts-expect-error", r"@ts-expect-error"),
    ("@ts-nocheck", r"@ts-nocheck"),
    ("prettier-ignore", r"prettier-ignore"),
    ("istanbul ignore", r"istanbul[ \t]+ignore(?:[ \t]+(?:next|if|else|file|start|stop))?"),
    ("c8 ignore", r"c8[ \t]+ignore(?:[ \t]+(?:next(?:[ \t]+[0-9]+)?|start|stop|file))?"),
    ("shellcheck disable", r"shellcheck[ \t]+disable=[A-Za-z0-9,]+"),
)
MARKER_NAMES = tuple(name for name, _ in _MARKERS)
_MARKER_RE = re.compile(
    "|".join(f"(?P<m{index}>{pattern})(?![\\w-])" for index, (_, pattern) in enumerate(_MARKERS)), re.IGNORECASE
)
_HASH_SPLIT_RE = re.compile(r"#+[ \t]*")
_TRAILER_RE = re.compile(r"^(?:-{2,}|#+|—)?\s*(?P<reason>.*?)\s*·\s*만료\s+(?P<date>\S+)\s*$", re.DOTALL)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REASON_PREFIX = "검사 끄기:"
_TS_VALUE = r"""(?:'[^'\n]*'|"[^"\n]*")"""
_TS_DIRECTIVE_RE = re.compile(
    rf"^///[ \t]*<(?:reference[ \t]+(?:path|types|lib|no-default-lib)[ \t]*=[ \t]*{_TS_VALUE}"
    rf"|amd-module[ \t]+name[ \t]*=[ \t]*{_TS_VALUE}"
    rf"|amd-dependency[ \t]+path[ \t]*=[ \t]*{_TS_VALUE}(?:[ \t]+name[ \t]*=[ \t]*{_TS_VALUE})?)[ \t]*/>[ \t]*$"
)
_ISSUE_PATH_RE = re.compile(r"/issues/\d+(?:/|$)")


@dataclass(frozen=True)
class Comment:
    line: int
    end_line: int
    body: str
    category: str
    orphan: bool = False
    malformed: bool = False


@dataclass(frozen=True)
class Suppression:
    line: int
    marker: str
    key: str
    reason: str | None
    expiry: date | None
    problem: str | None
    form: str


@dataclass(frozen=True)
class FileScan:
    path: str
    comments: tuple[Comment, ...] = ()
    suppressions: tuple[Suppression, ...] = ()
    error: LexError | None = None

    @property
    def disallowed(self) -> list[Comment]:
        return [comment for comment in self.comments if comment.category == DISALLOWED]


@dataclass
class _Hit:
    marker: str
    key: str
    trailer: str


@dataclass
class _Unit:
    raw: RawComment
    category: str
    hits: list[_Hit] = field(default_factory=list)


class ToolMissing(Exception):
    pass


def language_of(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.name.startswith("Dockerfile"):
        return "dockerfile"
    return LANGUAGES.get(pure.suffix)


def is_excluded(path: str, patterns: Sequence[str]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.full_match(pattern) for pattern in patterns)


def is_target(path: str, patterns: Sequence[str] = ()) -> bool:
    return language_of(path) is not None and not is_excluded(path, patterns)


def classify_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("외부: ") and stripped[4:].strip():
        return EXTERNAL
    if stripped.startswith("라이선스"):
        return LICENSE
    if stripped.startswith(_REASON_PREFIX):
        return SUPPRESSION_REASON
    if stripped.startswith("https://"):
        parts = urlsplit(stripped.split()[0])
        if parts.netloc and _ISSUE_PATH_RE.search(parts.path):
            return ISSUE
    return DISALLOWED


def _marker_at(text: str, position: int) -> re.Match[str] | None:
    return _MARKER_RE.match(text, position)


def _marker_name(match: re.Match[str]) -> str:
    return MARKER_NAMES[int(str(match.lastgroup)[1:])]


def _hits(body: str, hash_style: bool) -> tuple[int, list[tuple[int, int, str, str]]]:
    starts = [0]
    if hash_style:
        starts.extend(match.end() for match in _HASH_SPLIT_RE.finditer(body))
    found: list[tuple[int, int, str, str]] = []
    for start in starts:
        match = _marker_at(body, start)
        if match is None or (found and start < found[-1][1]):
            continue
        key = " ".join(match.group(0).split())
        found.append((match.start(), match.end(), _marker_name(match), key))
    first = found[0][0] if found else len(body)
    return first, found


def _analyze(raw: RawComment) -> _Unit:
    hash_style = raw.style == "hash"
    body = raw.body.lstrip() if raw.style != "doc" else raw.body.strip()
    if raw.style == "doc":
        return _Unit(raw, classify_text(body))
    flat = " ".join(body.split()) if raw.style == "block" else body
    first, found = _hits(flat, hash_style)
    lead = flat[:first].rstrip().rstrip("#").strip()
    category = SUPPRESSION if first == 0 and found else classify_text(lead)
    hits: list[_Hit] = []
    for index, (_, end, name, key) in enumerate(found):
        stop = found[index + 1][0] if index + 1 < len(found) else len(flat)
        hits.append(_Hit(name, key, flat[end:stop].strip()))
    for index in range(len(hits) - 2, -1, -1):
        if not hits[index].trailer.strip("#- \t"):
            hits[index].trailer = hits[index + 1].trailer
    return _Unit(raw, category, hits)


def parse_trailer(trailer: str) -> tuple[str | None, date | None, str | None]:
    match = _TRAILER_RE.match(trailer.strip())
    if match is None or not match.group("reason").strip():
        return None, None, MISSING
    reason = match.group("reason").strip()
    text = match.group("date")
    if not _DATE_RE.fullmatch(text):
        return reason, None, BAD_DATE
    try:
        return reason, date.fromisoformat(text), None
    except ValueError:
        return reason, None, BAD_DATE


def _above_reason(unit: _Unit) -> str:
    return unit.raw.body.strip()[len(_REASON_PREFIX) :].strip()


def _build(path: str, raws: Sequence[RawComment]) -> FileScan:
    units = [_analyze(raw) for raw in raws]
    reasons = {unit.raw.end_line: unit for unit in units if unit.category == SUPPRESSION_REASON}
    marker_lines = {unit.raw.line for unit in units if unit.hits}
    comments: list[Comment] = []
    suppressions: list[Suppression] = []
    for unit in units:
        if unit.category == SUPPRESSION_REASON and unit.raw.end_line + 1 not in marker_lines:
            comments.append(Comment(unit.raw.line, unit.raw.end_line, unit.raw.body, DISALLOWED, orphan=True))
        elif unit.category == SUPPRESSION_REASON and parse_trailer(_above_reason(unit))[2] is not None:
            comments.append(Comment(unit.raw.line, unit.raw.end_line, unit.raw.body, DISALLOWED, malformed=True))
        else:
            comments.append(Comment(unit.raw.line, unit.raw.end_line, unit.raw.body, unit.category))
        above = reasons.get(unit.raw.line - 1)
        for hit in unit.hits:
            reason, expiry, problem = parse_trailer(hit.trailer)
            form = "inline"
            if problem == MISSING and above is not None:
                reason, expiry, problem = parse_trailer(_above_reason(above))
                form = "above"
            suppressions.append(Suppression(unit.raw.line, hit.marker, hit.key, reason, expiry, problem, form))
    return FileScan(path, tuple(comments), tuple(suppressions))


def _ts_raws(entries: Sequence[dict]) -> list[RawComment]:
    raws: list[RawComment] = []
    for entry in entries:
        text = entry["text"]
        if entry["style"] == "line":
            if entry.get("leading") and _TS_DIRECTIVE_RE.match(text):
                continue
            raws.append(RawComment(entry["line"], entry["endLine"], lexers.line_body(text), "line"))
        else:
            raws.append(RawComment(entry["line"], entry["endLine"], lexers.block_body(text), "block"))
    return raws


def _run_ts(items: Sequence[tuple[str, str]], ts_lib: Path | None) -> list[dict]:
    if ts_lib is None:
        raise ToolMissing(
            "TS/JS 대상 파일이 있는데 trust.toml [trust] node_modules 가 없다 — "
            "typescript 가 설치된 node_modules 경로(뿌리 기준)를 적고 bun install --frozen-lockfile 을 먼저 한다"
        )
    bun = shutil.which("bun")
    if bun is None:
        raise ToolMissing("bun 이 없다 — bun 을 설치하고 TS 의존을 bun install --frozen-lockfile 로 먼저 설치한다")
    if not ts_lib.is_file():
        raise ToolMissing(
            f"{ts_lib} 가 없다 — trust.toml [trust] node_modules 가 가리키는 폴더에서 bun install --frozen-lockfile 을 먼저 한다"
        )
    payload = json.dumps([{"name": name, "text": text} for name, text in items])
    result = subprocess.run(
        [bun, str(TS_HELPER), str(ts_lib)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ToolMissing(f"TS 주석 판정기가 실패했다: {result.stderr.strip()}")
    return json.loads(result.stdout)


_LEXERS = {
    "python": lexers.python_comments,
    "shell": lexers.shell_comments,
    "yaml": lexers.yaml_comments,
    "toml": lexers.toml_comments,
    "rust": lexers.rust_comments,
    "dockerfile": lexers.dockerfile_comments,
}


def scan_texts(items: Sequence[tuple[str, str]], ts_lib: Path | None = None) -> list[FileScan]:
    results: list[FileScan | None] = [None] * len(items)
    ts_items: list[tuple[int, str, str]] = []
    for index, (path, text) in enumerate(items):
        language = language_of(path)
        if language is None:
            results[index] = FileScan(path)
        elif language == "ts":
            ts_items.append((index, path, text))
        else:
            try:
                results[index] = _build(path, _LEXERS[language](text))
            except LexError as error:
                results[index] = FileScan(path, error=error)
    if ts_items:
        outputs = _run_ts([(path, text) for _, path, text in ts_items], ts_lib)
        for (index, path, _), output in zip(ts_items, outputs, strict=True):
            if output["error"]:
                results[index] = FileScan(path, error=LexError(1, f"TS 판정 실패: {output['error']}"))
            else:
                results[index] = _build(path, _ts_raws(output["comments"]))
    return [result if result is not None else FileScan(path) for result, (path, _) in zip(results, items, strict=True)]


def scan_text(path: str, text: str, ts_lib: Path | None = None) -> FileScan:
    return scan_texts([(path, text)], ts_lib)[0]
