from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scripts.trust import gitfiles, settings
from scripts.trust.checks import LABEL, Problem
from scripts.trust.scan import (
    DISALLOWED,
    EXTERNAL,
    ISSUE,
    LICENSE,
    REPO_ROOT,
    FileScan,
    is_target,
    scan_texts,
)
from scripts.trust.settings import FRONTEND_API_RULES, eslint_level_problems

BASELINE_PATH = "backend/scripts/trust_baseline.json"
TY_BASELINE_PATH = "backend/ty-baseline.json"
PYPROJECT_PATH = "backend/pyproject.toml"
ESLINT_EXCEPTIONS_PATH = "frontend/eslint-exceptions.json"
STANDARDS_PATH = "docs/standards.md"
FRONTEND_SRC = "frontend/src/"
BACKEND_TESTS = "backend/tests/"
KNIP_PATH = "frontend/node_modules/.bin/knip"
ESLINT_CONFIG_PATH = "frontend/eslint.config.mjs"
ESLINT_HELPER = Path(__file__).resolve().with_name("eslint_rules.mjs")
TOOL_FRONTEND = REPO_ROOT / "frontend"
ESLINT_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})
QUARANTINE = "ci_quarantine"

KEYS = (
    "services_to_routes_imports",
    "direct_db_clients",
    "frontend_api_exceptions",
    "ts_any",
    "suppressions",
    "allowed_comments_external",
    "allowed_comments_license",
    "allowed_comments_issue",
    "disallowed_comments",
    "ty_baseline_entries",
    "knip_unused",
    "ci_quarantine",
    "prose_only_rules",
)
COMMENT_KEYS = (
    "suppressions",
    "allowed_comments_external",
    "allowed_comments_license",
    "allowed_comments_issue",
    "disallowed_comments",
)
_CATEGORY_KEYS = {
    EXTERNAL: "allowed_comments_external",
    LICENSE: "allowed_comments_license",
    ISSUE: "allowed_comments_issue",
    DISALLOWED: "disallowed_comments",
}
ALLOWED_DB_ENTRIES = frozenset({"core/database.py", "tests/**"})
TS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
STATUSES = frozenset({"강제됨", "판단(사람)", "문서뿐"})
PROSE_ONLY = "문서뿐"
TABLE_SECTION = "## 규칙과 강제 수단"
TABLE_HEADER = ["규칙", "출처", "강제 수단", "상태"]
REVERT_SUBJECT_RE = re.compile(r"^(?:Revert\b|revert(?:\(|:))")
_TS_ANY_RE = re.compile(r":\s*any\b|\bas\s+any\b|<any>")
_INSTALL_HINT = "cd frontend && bun install --frozen-lockfile 을 먼저 한다"


class MetricError(Exception):
    def __init__(self, path: str, line: int, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.line = line
        self.message = message


KnipRunner = Callable[[Path], str]
EslintRunner = Callable[[Path, list[str], list[str], list[dict[str, str]]], dict]


@dataclass
class CheckResult:
    compared: dict[str, int] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def _toml(text: str, path: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MetricError(path, getattr(error, "lineno", 1) or 1, f"TOML 을 읽지 못했다: {error}") from error


def _json(text: str, path: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise MetricError(path, error.lineno, f"JSON 을 읽지 못했다: {error.msg}") from error


def count_services_to_routes(text: str) -> int:
    contracts = _toml(text, PYPROJECT_PATH).get("tool", {}).get("importlinter", {}).get("contracts", [])
    matches = [
        contract
        for contract in contracts
        if isinstance(contract, dict)
        and contract.get("source_modules") == ["src.services"]
        and "src.routes" in contract.get("forbidden_modules", [])
    ]
    if len(matches) != 1:
        raise MetricError(PYPROJECT_PATH, 1, f"src.services ↛ src.routes 계약이 {len(matches)}개다(1개여야 한다)")
    ignored = matches[0].get("ignore_imports", [])
    if not isinstance(ignored, list):
        raise MetricError(PYPROJECT_PATH, 1, "계약 2 의 ignore_imports 가 목록이 아니다")
    return len(ignored)


def count_direct_db_clients(text: str) -> int:
    data = _toml(text, PYPROJECT_PATH)
    if not settings.has_per_file_ignores(data):
        raise MetricError(PYPROJECT_PATH, 1, "[tool.ruff.lint.per-file-ignores] 가 없다")
    return sum(1 for pattern in settings.tid251_exceptions(data) if pattern not in ALLOWED_DB_ENTRIES)


def count_frontend_api_exceptions(text: str) -> int:
    data = _json(text, ESLINT_EXCEPTIONS_PATH)
    if not isinstance(data, dict):
        raise MetricError(ESLINT_EXCEPTIONS_PATH, 1, "최상위가 객체가 아니다")
    total = 0
    for rule, paths in data.items():
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise MetricError(ESLINT_EXCEPTIONS_PATH, 1, f"'{rule}' 의 값이 경로 문자열 목록이 아니다")
        total += len(paths)
    return total


def count_ts_any(text: str) -> int:
    return len(_TS_ANY_RE.findall(text))


def count_ty_baseline(text: str) -> int:
    data = _json(text, TY_BASELINE_PATH)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise MetricError(TY_BASELINE_PATH, 1, "'files' 객체가 없다")
    total = 0
    for path, entries in files.items():
        if not isinstance(entries, list):
            raise MetricError(TY_BASELINE_PATH, 1, f"'{path}' 의 진단이 목록이 아니다")
        total += len(entries)
    return total


def count_knip(text: str) -> int:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise MetricError(KNIP_PATH, 1, f"knip 출력이 JSON 이 아니다: {error.msg}") from error
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list):
        raise MetricError(KNIP_PATH, 1, "knip 출력에 'issues' 목록이 없다")
    return sum(
        len(value) for issue in issues if isinstance(issue, dict) for value in issue.values() if isinstance(value, list)
    )


def count_ci_quarantine(text: str, path: str = BACKEND_TESTS) -> int:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise MetricError(path, error.lineno or 1, f"Python 문법 오류: {error.msg}") from error
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == QUARANTINE:
            count += 1
        elif isinstance(node, ast.Name) and node.id == QUARANTINE:
            count += 1
        elif isinstance(node, ast.alias) and QUARANTINE in (node.name.rsplit(".", 1)[-1], node.asname):
            count += 1
        elif isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            count += sum(
                1 for argument in arguments if isinstance(argument, ast.Constant) and argument.value == QUARANTINE
            )
    return count


def _cells(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and body[index + 1 : index + 2] == "|":
            current.append("|")
            index += 2
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def count_prose_only_rules(text: str) -> int:
    lines = text.split("\n")
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == TABLE_SECTION)
    except StopIteration:
        raise MetricError(STANDARDS_PATH, 1, f"'{TABLE_SECTION}' 절이 없다") from None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    rows = [(index + 1, lines[index]) for index in range(start + 1, end) if lines[index].lstrip().startswith("|")]
    if len(rows) < 2 or _cells(rows[0][1]) != TABLE_HEADER:
        raise MetricError(
            STANDARDS_PATH, start + 1, f"'{TABLE_SECTION}' 절에 '| 규칙 | 출처 | 강제 수단 | 상태 |' 표가 없다"
        )
    count = 0
    for number, line in rows[2:]:
        cells = _cells(line)
        if len(cells) != len(TABLE_HEADER):
            raise MetricError(STANDARDS_PATH, number, f"표 행의 칸이 {len(cells)}개다(4개여야 한다)")
        status = cells[3]
        if status not in STATUSES:
            raise MetricError(STANDARDS_PATH, number, f"상태 '{status}' 는 강제됨·판단(사람)·문서뿐 중 하나가 아니다")
        count += status == PROSE_ONLY
    return count


def comment_counts(scans: Iterable[FileScan]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(COMMENT_KEYS, 0)
    for result in scans:
        counts["suppressions"] += len(result.suppressions)
        for comment in result.comments:
            key = _CATEGORY_KEYS.get(comment.category)
            if key is not None:
                counts[key] += 1
    return dict(sorted(counts.items()))


def run_knip(root: Path) -> str:
    binary = root / KNIP_PATH
    if not binary.is_file():
        raise MetricError(KNIP_PATH, 1, f"knip 이 없다 — {_INSTALL_HINT}")
    result = subprocess.run(
        [str(binary), "--reporter", "json"], cwd=root / "frontend", capture_output=True, text=True, check=False
    )
    if result.returncode not in (0, 1):
        raise MetricError(
            KNIP_PATH, 1, f"knip 이 실패했다(종료 코드 {result.returncode}): {result.stderr.strip()[-300:]}"
        )
    return result.stdout


def repo_files(root: Path) -> list[str]:
    paths = set(gitfiles.tracked_files(root)) | set(gitfiles.untracked_files(root))
    return sorted(path for path in paths if (root / path).is_file() and not (root / path).is_symlink())


def _read(root: Path, path: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MetricError(path, 1, "파일이 없다") from None
    except UnicodeDecodeError:
        raise MetricError(path, 1, "UTF-8 로 읽지 못했다") from None


def _source_problem(key: str, error: MetricError) -> Problem:
    return Problem(
        error.path, error.line, f"{key} 를 셀 수 없다({error.message}) — 원천 파일을 고친다. 0 으로 두지 않는다"
    )


def _comment_metrics(root: Path, files: list[str]) -> tuple[dict[str, int] | None, list[Problem]]:
    items: list[tuple[str, str]] = []
    problems: list[Problem] = []
    for path in files:
        if not is_target(path):
            continue
        try:
            items.append((path, _read(root, path)))
        except MetricError as error:
            problems.append(Problem(path, 1, f"주석 수를 셀 수 없다({error.message}) — 파일을 UTF-8 로 저장한다"))
    scans = scan_texts(items)
    for result in scans:
        if result.error is not None:
            problems.append(
                Problem(
                    result.path,
                    result.error.line,
                    f"주석 수를 셀 수 없다({result.error.message}) — 문법 오류를 고친다",
                )
            )
    return (None if problems else comment_counts(scans)), problems


def _sum_files(
    root: Path, files: list[str], prefix: str, suffixes: frozenset[str], counter: Callable[[str, str], int]
) -> int:
    return sum(
        counter(_read(root, path), path)
        for path in files
        if path.startswith(prefix) and PurePosixPath(path).suffix in suffixes
    )


def collect(root: Path, knip: KnipRunner = run_knip) -> tuple[dict[str, int | None], list[Problem]]:
    files = repo_files(root)
    values: dict[str, int | None] = dict.fromkeys(KEYS)
    problems: list[Problem] = []
    single: dict[str, Callable[[], int]] = {
        "services_to_routes_imports": lambda: count_services_to_routes(_read(root, PYPROJECT_PATH)),
        "direct_db_clients": lambda: count_direct_db_clients(_read(root, PYPROJECT_PATH)),
        "frontend_api_exceptions": lambda: count_frontend_api_exceptions(_read(root, ESLINT_EXCEPTIONS_PATH)),
        "ts_any": lambda: _sum_files(root, files, FRONTEND_SRC, TS_SUFFIXES, lambda text, _: count_ts_any(text)),
        "ty_baseline_entries": lambda: count_ty_baseline(_read(root, TY_BASELINE_PATH)),
        "knip_unused": lambda: count_knip(knip(root)),
        "ci_quarantine": lambda: _sum_files(root, files, BACKEND_TESTS, frozenset({".py"}), count_ci_quarantine),
        "prose_only_rules": lambda: count_prose_only_rules(_read(root, STANDARDS_PATH)),
    }
    for key, compute in single.items():
        try:
            values[key] = compute()
        except MetricError as error:
            problems.append(_source_problem(key, error))
    counts, comment_problems = _comment_metrics(root, files)
    problems.extend(comment_problems)
    if counts is not None:
        values.update(counts)
    return values, problems


def render_baseline(values: Mapping[str, int]) -> str:
    return json.dumps(dict(sorted(values.items())), ensure_ascii=False, indent=2) + "\n"


def _key_line(text: str, key: str) -> int:
    for number, line in enumerate(text.split("\n"), start=1):
        if f'"{key}"' in line:
            return number
    return 1


def parse_baseline(text: str, label: str) -> tuple[dict[str, int], list[Problem]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        return {}, [
            Problem(label, error.lineno, f"기준선 JSON 을 읽지 못했다({error.msg}) — metrics --update 로 다시 만든다")
        ]
    if not isinstance(data, dict):
        return {}, [Problem(label, 1, "기준선이 JSON 객체가 아니다 — metrics --update 로 다시 만든다")]
    values: dict[str, int] = {}
    problems: list[Problem] = []
    for key in KEYS:
        if key not in data:
            problems.append(Problem(label, 1, f"기준선에 열쇠 {key} 가 없다 — metrics --update 로 다시 만든다"))
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(
                Problem(
                    label, _key_line(text, key), f"기준선 {key} 값 {value!r} 는 0 이상 정수가 아니다 — 정수로 고친다"
                )
            )
            continue
        values[key] = value
    problems.extend(
        Problem(
            label,
            _key_line(text, key),
            f"기준선의 열쇠 {key} 는 모르는 열쇠다 — 지운다. 지표를 더하려면 metrics 코드에 먼저 더한다",
        )
        for key in data
        if key not in KEYS
    )
    return values, problems


def _verify_ref(root: Path, ref: str) -> None:
    gitfiles.git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def _show_text(root: Path, ref: str, path: str) -> str | None:
    data = gitfiles.show(root, f"{ref}:{path}")
    return None if data is None else data.decode("utf-8", "replace")


def _read_optional(root: Path, path: str) -> str | None:
    file = root / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def _approval_hint() -> str:
    return f"꼭 늘려야 하면 PR 에 사람이 {LABEL} 표지를 단다(에이전트는 달지 않는다)"


def _compare_current(
    result: CheckResult, current: Mapping[str, int | None], reference: Mapping[str, int], text: str, is_approved: bool
) -> None:
    for key in KEYS:
        now = current.get(key)
        before = reference.get(key)
        if now is None or before is None:
            continue
        if now > before:
            message = f"{key} 가 기준선 {before} 보다 늘었다(현재 {now}) — 늘린 원인을 되돌린다. {_approval_hint()}"
            if is_approved:
                result.notices.append(f"알림: {key} 증가({before} → {now})는 {LABEL} 표지로 통과한다")
            else:
                result.problems.append(Problem(BASELINE_PATH, _key_line(text, key), message))
        elif now < before:
            result.notices.append(
                f"알림: {key} 가 기준선 {before} 보다 줄었다(현재 {now}) — 원하면 metrics --update 로 기준선을 낮춘다"
            )


def _compare_baselines(
    result: CheckResult, worktree: Mapping[str, int], base: Mapping[str, int], text: str, is_approved: bool
) -> None:
    for key in KEYS:
        mine = worktree.get(key)
        theirs = base.get(key)
        if mine is None or theirs is None or mine <= theirs:
            continue
        if is_approved:
            result.notices.append(f"알림: 기준선 {key} 올림({theirs} → {mine})은 {LABEL} 표지로 통과한다")
        else:
            result.problems.append(
                Problem(
                    BASELINE_PATH,
                    _key_line(text, key),
                    f"PR 의 기준선 값 {key}={mine} 가 기준 ref 값 {theirs} 보다 크다 — 되돌린다. {_approval_hint()}",
                )
            )


def _compare_ty(result: CheckResult, root: Path, ref: str, is_approved: bool) -> None:
    base_text, effective = _baseline_source(root, ref, TY_BASELINE_PATH)
    work_text = _read_optional(root, TY_BASELINE_PATH)
    if base_text is None or effective is None:
        result.notices.append(f"알림: 기준 ref 에 {TY_BASELINE_PATH} 가 없어 ty 기준선 항목 수는 비교하지 않는다")
        return
    if effective != ref:
        result.notices.append(
            f"알림: 기준 ref({ref})에는 {TY_BASELINE_PATH} 가 없다 — 지워진 ty 기준선({effective})과 비교한다"
        )
    if work_text is None:
        result.problems.append(
            Problem(TY_BASELINE_PATH, 1, _deleted_message(ref, effective, TY_BASELINE_PATH, "ty 기준선"))
        )
        return
    try:
        before = count_ty_baseline(base_text)
        after = count_ty_baseline(work_text)
    except MetricError as error:
        result.problems.append(_source_problem("ty_baseline_entries", error))
        return
    if after <= before:
        return
    if is_approved:
        result.notices.append(f"알림: ty 기준선 항목 증가({before} → {after})는 {LABEL} 표지로 통과한다")
        return
    result.problems.append(
        Problem(
            TY_BASELINE_PATH,
            1,
            f"ty 기준선 진단 항목이 기준 ref 보다 늘었다({before} → {after}) — 새 타입 오류를 고치고 기준선을 되돌린다. "
            f"{_approval_hint()}",
        )
    )


def _baseline_source(root: Path, ref: str, path: str) -> tuple[str | None, str | None]:
    text = _show_text(root, ref, path)
    if text is not None:
        return text, ref
    last = gitfiles.git(root, "log", "-1", "--format=%H", ref, "--", path).decode().strip()
    if not last:
        return None, None
    parent = f"{last}^"
    previous = _show_text(root, parent, path)
    return (previous, parent) if previous is not None else (None, None)


def _gate(result: CheckResult, problems: list[Problem], is_approved: bool) -> None:
    if is_approved:
        result.notices.extend(f"알림: {LABEL} 표지로 통과한다 — {problem.render()}" for problem in problems)
    else:
        result.problems.extend(problems)


def _parse_toml(text: str | None) -> dict | None:
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _parse_json(text: str | None) -> object:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def frontend_files(root: Path, files: Iterable[str]) -> list[str]:
    return [
        path.removeprefix("frontend/")
        for path in files
        if path.startswith(FRONTEND_SRC) and PurePosixPath(path).suffix in ESLINT_SUFFIXES
    ]


def run_eslint(
    root: Path, files: list[str], rules: Iterable[str] = FRONTEND_API_RULES, probes: Iterable[dict[str, str]] = ()
) -> dict:
    bun = shutil.which("bun")
    if bun is None:
        raise MetricError(ESLINT_CONFIG_PATH, 1, f"bun 이 없다 — {_INSTALL_HINT}")
    if not (TOOL_FRONTEND / "node_modules/eslint/package.json").is_file():
        raise MetricError(ESLINT_CONFIG_PATH, 1, f"eslint 이 없다 — {_INSTALL_HINT}")
    result = subprocess.run(
        [bun, str(ESLINT_HELPER), str(TOOL_FRONTEND), str(root / "frontend")],
        input=json.dumps({"files": files, "rules": list(rules), "probes": list(probes)}),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MetricError(ESLINT_CONFIG_PATH, 1, f"eslint 설정 계산이 실패했다: {result.stderr.strip()[-300:]}")
    return json.loads(result.stdout)


def _settings(result: CheckResult, root: Path, base_ref: str | None, is_approved: bool, eslint: EslintRunner) -> None:
    gated: list[Problem] = []
    work_text = _read_optional(root, PYPROJECT_PATH)
    work = _parse_toml(work_text)
    if work_text is not None and work is not None:
        gated.extend(settings.pyproject_absolute_problems(work_text, work, root))
        base = _parse_toml(_show_text(root, base_ref, PYPROJECT_PATH)) if base_ref else None
        if base is not None:
            gated.extend(settings.pyproject_relative_problems(work_text, base, work))
    exceptions_text = _read_optional(root, ESLINT_EXCEPTIONS_PATH)
    exceptions = _parse_json(exceptions_text)
    if base_ref and exceptions_text is not None:
        base_exceptions = _parse_json(_show_text(root, base_ref, ESLINT_EXCEPTIONS_PATH))
        if base_exceptions is not None:
            gated.extend(settings.eslint_exception_problems(exceptions_text, base_exceptions, exceptions))
    exception_map = exceptions if isinstance(exceptions, dict) else {}
    rules = list(dict.fromkeys([*FRONTEND_API_RULES, *exception_map]))
    files = frontend_files(root, repo_files(root))
    if files:
        try:
            results = eslint(root, files, rules, settings.eslint_probes(files))
        except MetricError as error:
            result.problems.append(
                Problem(error.path, error.line, f"eslint 설정을 확인하지 못했다({error.message}) — {_INSTALL_HINT}")
            )
        else:
            gated.extend(eslint_level_problems(results.get("files", {}), exception_map, rules))
            gated.extend(settings.eslint_contract_problems(results.get("probes", []), exception_map))
    _gate(result, gated, is_approved)


def _deleted_message(ref: str, effective: str, path: str, label: str) -> str:
    if effective == ref:
        where = f"기준 ref({ref})에 있던 {label}을 지웠다. git show {ref}:{path} 로 되살린다"
    else:
        where = f"지워진 {label}을 git show {effective}:{path} 로 되살린다"
    return f"{label} 파일이 없다 — {where}. {label} 삭제는 {LABEL} 표지로도 통과하지 않는다"


def check(
    root: Path, ref: str, current: Mapping[str, int | None], is_approved: bool, eslint: EslintRunner = run_eslint
) -> CheckResult:
    _verify_ref(root, ref)
    result = CheckResult()
    work_text = _read_optional(root, BASELINE_PATH)
    worktree: dict[str, int] = {}
    if work_text is not None:
        worktree, problems = parse_baseline(work_text, BASELINE_PATH)
        result.problems.extend(problems)
    base_text, effective = _baseline_source(root, ref, BASELINE_PATH)
    if base_text is None or effective is None:
        result.notices.append(f"알림: 기준 ref 에 {BASELINE_PATH} 이 없어 작업 트리 파일과 비교한다")
        if work_text is None:
            result.problems.append(Problem(BASELINE_PATH, 1, "기준선 파일이 없다 — metrics --update 로 만든다"))
        else:
            result.compared = worktree
            _compare_current(result, current, worktree, work_text, is_approved)
        _compare_ty(result, root, ref, is_approved)
        _settings(result, root, None, is_approved, eslint)
        return result
    if effective != ref:
        result.notices.append(
            f"알림: 기준 ref({ref})에는 {BASELINE_PATH} 이 없다 — 지워진 기준선({effective})과 비교한다"
        )
    if work_text is None:
        result.problems.append(Problem(BASELINE_PATH, 1, _deleted_message(ref, effective, BASELINE_PATH, "기준선")))
    base, problems = parse_baseline(base_text, f"{effective}:{BASELINE_PATH}")
    result.problems.extend(problems)
    result.compared = base
    _compare_current(result, current, base, work_text or base_text, is_approved)
    _compare_baselines(result, worktree, base, work_text or base_text, is_approved)
    _compare_ty(result, root, ref, is_approved)
    _settings(result, root, effective, is_approved, eslint)
    return result


def worktree_baseline(root: Path) -> dict[str, int]:
    text = _read_optional(root, BASELINE_PATH)
    return {} if text is None else parse_baseline(text, BASELINE_PATH)[0]


def write_baseline(root: Path, values: Mapping[str, int]) -> Path:
    path = root / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_baseline(values), encoding="utf-8")
    return path


def revert_ratio(root: Path) -> tuple[int, int]:
    subjects = gitfiles.git(root, "log", "--since=6.months", "--format=%s").decode("utf-8", "replace").splitlines()
    merges = gitfiles.git(root, "log", "--since=6.months", "--merges", "--format=%H").decode().split()
    return sum(1 for subject in subjects if REVERT_SUBJECT_RE.match(subject)), len(merges)


def metric_lines(current: Mapping[str, int | None], baseline: Mapping[str, int]) -> list[str]:
    lines = []
    for key in KEYS:
        now = current.get(key)
        shown = "읽지 못함" if now is None else str(now)
        before = baseline.get(key)
        lines.append(f"{key}={shown} (기준선 {'없음' if before is None else before})")
    return lines


def revert_line(root: Path) -> str:
    try:
        reverts, merges = revert_ratio(root)
    except gitfiles.GitError as error:
        return f"revert_ratio_6m=? (git 을 읽지 못했다: {error})"
    ratio = reverts / merges if merges else 0.0
    return f"revert_ratio_6m={ratio:.4f} (되돌림 {reverts} / 머지 {merges})"
