from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from trust_checks import config as config_module
from trust_checks import gitfiles, settings
from trust_checks.checks import LABEL, Problem
from trust_checks.config import (
    COMMENT_KEYS,
    BannedApi,
    Config,
    ConfigError,
    Eslint,
    ImportLinter,
    Knip,
    ProseOnly,
    Quarantine,
    TsAny,
    Ty,
    join,
    under,
)
from trust_checks.scan import DISALLOWED, EXTERNAL, ISSUE, LICENSE, FileScan, is_target, scan_texts
from trust_checks.settings import ContractError, EslintContract

ESLINT_HELPER = Path(__file__).resolve().with_name("eslint_rules.mjs")
TS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
STATUSES = frozenset({"강제됨", "판단(사람)", "문서뿐"})
PROSE_ONLY = "문서뿐"
TABLE_HEADER = ["규칙", "출처", "강제 수단", "상태"]
REVERT_SUBJECT_RE = re.compile(r"^(?:Revert\b|revert(?:\(|:))")
_TS_ANY_RE = re.compile(r":\s*any\b|\bas\s+any\b|<any>")
_CATEGORY_KEYS = {
    EXTERNAL: "allowed_comments_external",
    LICENSE: "allowed_comments_license",
    ISSUE: "allowed_comments_issue",
    DISALLOWED: "disallowed_comments",
}


class MetricError(Exception):
    def __init__(self, path: str, line: int, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.line = line
        self.message = message


KnipRunner = Callable[[Path, str], str]
EslintRunner = Callable[[Path, str, list[str], list[str], list[dict[str, str]]], dict]


@dataclass
class CheckResult:
    compared: dict[str, int] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def install_hint(directory: str) -> str:
    return f"cd {directory or '.'} && bun install --frozen-lockfile 을 먼저 한다"


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


def count_import_linter(text: str, path: str, name: str) -> int:
    contracts = _toml(text, path).get("tool", {}).get("importlinter", {}).get("contracts", [])
    matches = [contract for contract in contracts if isinstance(contract, dict) and contract.get("name") == name]
    if len(matches) != 1:
        raise MetricError(
            path,
            1,
            f"import-linter 계약 '{name}' 이 {len(matches)}개다(1개여야 한다) — "
            "trust.toml [metrics.import_linter] contract 를 계약 name 과 맞춘다",
        )
    ignored = matches[0].get("ignore_imports", [])
    if not isinstance(ignored, list):
        raise MetricError(path, 1, f"계약 '{name}' 의 ignore_imports 가 목록이 아니다")
    return len(ignored)


def count_banned_api(text: str, path: str, code: str, allow: Iterable[str] = ()) -> int:
    data = _toml(text, path)
    if not settings.has_per_file_ignores(data):
        raise MetricError(path, 1, "[tool.ruff.lint.per-file-ignores] 가 없다")
    allowed = set(allow)
    return sum(1 for pattern in settings.code_exceptions(data, code) if pattern not in allowed)


def count_eslint_exceptions(text: str, path: str) -> int:
    data = _json(text, path)
    if not isinstance(data, dict):
        raise MetricError(path, 1, "최상위가 객체가 아니다")
    total = 0
    for rule, paths in data.items():
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise MetricError(path, 1, f"'{rule}' 의 값이 경로 문자열 목록이 아니다")
        total += len(paths)
    return total


def count_ts_any(text: str) -> int:
    return len(_TS_ANY_RE.findall(text))


def count_ty_baseline(text: str, path: str) -> int:
    data = _json(text, path)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise MetricError(path, 1, "'files' 객체가 없다")
    total = 0
    for name, entries in files.items():
        if not isinstance(entries, list):
            raise MetricError(path, 1, f"'{name}' 의 진단이 목록이 아니다")
        total += len(entries)
    return total


def count_knip(text: str, path: str) -> int:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise MetricError(path, 1, f"knip 출력이 JSON 이 아니다: {error.msg}") from error
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list):
        raise MetricError(path, 1, "knip 출력에 'issues' 목록이 없다")
    return sum(
        len(value) for issue in issues if isinstance(issue, dict) for value in issue.values() if isinstance(value, list)
    )


def count_ci_quarantine(text: str, path: str = "", marker: str = "ci_quarantine") -> int:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise MetricError(path, error.lineno or 1, f"Python 문법 오류: {error.msg}") from error
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == marker:
            count += 1
        elif isinstance(node, ast.Name) and node.id == marker:
            count += 1
        elif isinstance(node, ast.alias) and marker in (node.name.rsplit(".", 1)[-1], node.asname):
            count += 1
        elif isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            count += sum(1 for argument in arguments if isinstance(argument, ast.Constant) and argument.value == marker)
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


def count_prose_only_rules(text: str, path: str, heading: str) -> int:
    section = f"## {heading}"
    lines = text.split("\n")
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == section)
    except StopIteration:
        raise MetricError(path, 1, f"'{section}' 절이 없다") from None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    rows = [(index + 1, lines[index]) for index in range(start + 1, end) if lines[index].lstrip().startswith("|")]
    if len(rows) < 2 or _cells(rows[0][1]) != TABLE_HEADER:
        raise MetricError(path, start + 1, f"'{section}' 절에 '| 규칙 | 출처 | 강제 수단 | 상태 |' 표가 없다")
    count = 0
    for number, line in rows[2:]:
        cells = _cells(line)
        if len(cells) != len(TABLE_HEADER):
            raise MetricError(path, number, f"표 행의 칸이 {len(cells)}개다(4개여야 한다)")
        status = cells[3]
        if status not in STATUSES:
            raise MetricError(path, number, f"상태 '{status}' 는 강제됨·판단(사람)·문서뿐 중 하나가 아니다")
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


def _find_up(root: Path, directory: str, relative: str) -> Path | None:
    top = root.resolve()
    current = (root / directory).resolve()
    while True:
        candidate = current / relative
        if candidate.exists():
            return candidate
        if current == top or current.parent == current:
            return None
        current = current.parent


def run_knip(root: Path, directory: str) -> str:
    label = join(directory, "node_modules/.bin/knip")
    binary = _find_up(root, directory, "node_modules/.bin/knip")
    if binary is None:
        raise MetricError(label, 1, f"knip 이 없다 — {install_hint(directory)}")
    result = subprocess.run(
        [str(binary), "--reporter", "json"], cwd=root / directory, capture_output=True, text=True, check=False
    )
    if result.returncode not in (0, 1):
        raise MetricError(label, 1, f"knip 이 실패했다(종료 코드 {result.returncode}): {result.stderr.strip()[-300:]}")
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


def _comment_metrics(root: Path, config: Config, files: list[str]) -> tuple[dict[str, int] | None, list[Problem]]:
    items: list[tuple[str, str]] = []
    problems: list[Problem] = []
    for path in files:
        if not is_target(path, config.exclude):
            continue
        try:
            items.append((path, _read(root, path)))
        except MetricError as error:
            problems.append(Problem(path, 1, f"주석 수를 셀 수 없다({error.message}) — 파일을 UTF-8 로 저장한다"))
    scans = scan_texts(items, config.ts_lib(root))
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
    root: Path,
    files: list[str],
    directories: Sequence[str],
    suffixes: frozenset[str],
    counter: Callable[[str, str], int],
) -> int:
    return sum(
        counter(_read(root, path), path)
        for path in files
        if any(under(path, directory) for directory in directories) and PurePosixPath(path).suffix in suffixes
    )


def _computers(root: Path, config: Config, files: list[str], knip: KnipRunner) -> list[tuple[str, Callable[[], int]]]:
    found: list[tuple[str, Callable[[], int]]] = []
    if (linter := config.get(ImportLinter)) is not None:
        found.append(
            (linter.key, lambda: count_import_linter(_read(root, linter.pyproject), linter.pyproject, linter.contract))
        )
    if (banned := config.get(BannedApi)) is not None:
        found.append(
            (
                banned.key,
                lambda: count_banned_api(_read(root, banned.pyproject), banned.pyproject, banned.code, banned.allow),
            )
        )
    if (eslint := config.get(Eslint)) is not None:
        found.append((eslint.key, lambda: count_eslint_exceptions(_read(root, eslint.exceptions), eslint.exceptions)))
    if (ts_any := config.get(TsAny)) is not None:
        found.append(
            (ts_any.key, lambda: _sum_files(root, files, ts_any.roots, TS_SUFFIXES, lambda text, _: count_ts_any(text)))
        )
    if (ty := config.get(Ty)) is not None:
        found.append((ty.key, lambda: count_ty_baseline(_read(root, ty.baseline), ty.baseline)))
    if (knip_section := config.get(Knip)) is not None:
        found.append(
            (
                knip_section.key,
                lambda: count_knip(knip(root, knip_section.dir), join(knip_section.dir, "node_modules/.bin/knip")),
            )
        )
    if (quarantine := config.get(Quarantine)) is not None:
        found.append(
            (
                quarantine.key,
                lambda: _sum_files(
                    root,
                    files,
                    (quarantine.tests,),
                    frozenset({".py"}),
                    lambda text, path: count_ci_quarantine(text, path, quarantine.marker),
                ),
            )
        )
    if (prose := config.get(ProseOnly)) is not None:
        found.append((prose.key, lambda: count_prose_only_rules(_read(root, prose.doc), prose.doc, prose.heading)))
    return found


def collect(root: Path, config: Config, knip: KnipRunner = run_knip) -> tuple[dict[str, int | None], list[Problem]]:
    files = repo_files(root)
    values: dict[str, int | None] = dict.fromkeys(config.metric_keys())
    problems: list[Problem] = []
    for key, compute in _computers(root, config, files, knip):
        try:
            values[key] = compute()
        except MetricError as error:
            problems.append(_source_problem(key, error))
    counts, comment_problems = _comment_metrics(root, config, files)
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


def parse_values(text: str, label: str) -> tuple[dict[str, int], set[str], list[Problem]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        return (
            {},
            set(),
            [
                Problem(
                    label, error.lineno, f"기준선 JSON 을 읽지 못했다({error.msg}) — metrics --update 로 다시 만든다"
                )
            ],
        )
    if not isinstance(data, dict):
        return {}, set(), [Problem(label, 1, "기준선이 JSON 객체가 아니다 — metrics --update 로 다시 만든다")]
    values: dict[str, int] = {}
    problems: list[Problem] = []
    for key, value in data.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(
                Problem(
                    label, _key_line(text, key), f"기준선 {key} 값 {value!r} 는 0 이상 정수가 아니다 — 정수로 고친다"
                )
            )
            continue
        values[key] = value
    return values, set(data), problems


def parse_baseline(text: str, label: str, keys: Sequence[str]) -> tuple[dict[str, int], list[Problem]]:
    values, present, problems = parse_values(text, label)
    if not present and problems:
        return {}, problems
    known = set(keys)
    missing = [
        Problem(label, 1, f"기준선에 열쇠 {key} 가 없다 — metrics --update 로 다시 만든다")
        for key in keys
        if key not in present
    ]
    unknown = [
        Problem(
            label,
            _key_line(text, key),
            f"기준선의 열쇠 {key} 는 켜진 지표가 아니다 — 지운다. 지표를 켜려면 trust.toml 에 [metrics.…] 절을 더한다",
        )
        for key in present
        if key not in known
    ]
    bad = [problem for problem in problems if any(f"기준선 {key} 값" in problem.message for key in known)]
    return {key: value for key, value in values.items() if key in known}, [*missing, *bad, *unknown]


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
    result: CheckResult,
    keys: Sequence[str],
    current: Mapping[str, int | None],
    reference: Mapping[str, int],
    path: str,
    text: str,
    is_approved: bool,
) -> None:
    for key in keys:
        now = current.get(key)
        before = reference.get(key)
        if now is None or before is None:
            continue
        if now > before:
            message = f"{key} 가 기준선 {before} 보다 늘었다(현재 {now}) — 늘린 원인을 되돌린다. {_approval_hint()}"
            if is_approved:
                result.notices.append(f"알림: {key} 증가({before} → {now})는 {LABEL} 표지로 통과한다")
            else:
                result.problems.append(Problem(path, _key_line(text, key), message))
        elif now < before:
            result.notices.append(
                f"알림: {key} 가 기준선 {before} 보다 줄었다(현재 {now}) — 원하면 metrics --update 로 기준선을 낮춘다"
            )


def _compare_baselines(
    result: CheckResult,
    keys: Sequence[str],
    worktree: Mapping[str, int],
    base: Mapping[str, int],
    path: str,
    text: str,
    is_approved: bool,
) -> None:
    for key in keys:
        mine = worktree.get(key)
        theirs = base.get(key)
        if mine is None or theirs is None or mine <= theirs:
            continue
        if is_approved:
            result.notices.append(f"알림: 기준선 {key} 올림({theirs} → {mine})은 {LABEL} 표지로 통과한다")
        else:
            result.problems.append(
                Problem(
                    path,
                    _key_line(text, key),
                    f"PR 의 기준선 값 {key}={mine} 가 기준 ref 값 {theirs} 보다 크다 — 되돌린다. {_approval_hint()}",
                )
            )


def _compare_ty(result: CheckResult, root: Path, ty: Ty, ref: str, is_approved: bool) -> None:
    path = ty.baseline
    base_text, effective = _baseline_source(root, ref, path)
    work_text = _read_optional(root, path)
    if base_text is None or effective is None:
        result.notices.append(f"알림: 기준 ref 에 {path} 가 없어 ty 기준선 항목 수는 비교하지 않는다")
        return
    if effective != ref:
        result.notices.append(f"알림: 기준 ref({ref})에는 {path} 가 없다 — 지워진 ty 기준선({effective})과 비교한다")
    if work_text is None:
        result.problems.append(Problem(path, 1, _deleted_message(ref, effective, path, "ty 기준선")))
        return
    try:
        before = count_ty_baseline(base_text, path)
        after = count_ty_baseline(work_text, path)
    except MetricError as error:
        result.problems.append(_source_problem(ty.key, error))
        return
    if after <= before:
        return
    if is_approved:
        result.notices.append(f"알림: ty 기준선 항목 증가({before} → {after})는 {LABEL} 표지로 통과한다")
        return
    result.problems.append(
        Problem(
            path,
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


def eslint_files(files: Iterable[str], directory: str, contract: EslintContract) -> list[str]:
    selected: list[str] = []
    for path in files:
        if not under(path, directory) or path == directory:
            continue
        relative = path[len(directory) + 1 :] if directory else path
        if contract.selects(relative):
            selected.append(relative)
    return selected


def run_eslint(root: Path, directory: str, files: list[str], rules: list[str], probes: list[dict[str, str]]) -> dict:
    label = settings.eslint_config_label(root, directory)
    bun = shutil.which("bun")
    if bun is None:
        raise MetricError(label, 1, f"bun 이 없다 — {install_hint(directory)}")
    if _find_up(root, directory, "node_modules/eslint/package.json") is None:
        raise MetricError(label, 1, f"eslint 이 없다 — {install_hint(directory)}")
    result = subprocess.run(
        [bun, str(ESLINT_HELPER), str(root / directory)],
        input=json.dumps({"files": files, "rules": rules, "probes": probes}),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MetricError(label, 1, f"eslint 설정 계산이 실패했다: {result.stderr.strip()[-300:]}")
    return json.loads(result.stdout)


def _pyproject_pair(root: Path, path: str, base_ref: str | None) -> tuple[str, dict, dict | None] | None:
    text = _read_optional(root, path)
    work = _parse_toml(text)
    if text is None or work is None:
        return None
    base = _parse_toml(_show_text(root, base_ref, path)) if base_ref else None
    return text, work, base


def _banned_api_problems(root: Path, section: BannedApi, base_ref: str | None) -> list[Problem]:
    pair = _pyproject_pair(root, section.pyproject, base_ref)
    if pair is None:
        return []
    text, work, base = pair
    project_dir = root / PurePosixPath(section.pyproject).parent
    found = settings.banned_api_absolute_problems(
        section.pyproject, text, work, project_dir, section.code, section.allow
    )
    if base is not None:
        found.extend(settings.banned_api_relative_problems(section.pyproject, text, base, work, section.code))
    return found


def _import_linter_problems(root: Path, section: ImportLinter, base_ref: str | None) -> list[Problem]:
    pair = _pyproject_pair(root, section.pyproject, base_ref)
    if pair is None:
        return []
    text, work, base = pair
    return [] if base is None else settings.import_linter_relative_problems(section.pyproject, text, base, work)


def _ty_setting_problems(root: Path, section: Ty, base_ref: str | None) -> list[Problem]:
    pair = _pyproject_pair(root, section.pyproject, base_ref)
    if pair is None:
        return []
    text, work, base = pair
    return [] if base is None else settings.ty_relative_problems(section.pyproject, text, base, work)


def _load_contract(root: Path, section: Eslint) -> tuple[EslintContract | None, str | None, list[Problem]]:
    text = _read_optional(root, section.contract)
    if text is None:
        return (
            None,
            None,
            [Problem(section.contract, 1, "eslint 계약 파일이 없다 — README 의 eslint 계약 절 모양으로 만든다")],
        )
    try:
        return settings.parse_contract(text), text, []
    except ContractError as error:
        return (
            None,
            text,
            [
                Problem(
                    section.contract,
                    1,
                    f"eslint 계약 파일 모양이 틀렸다({error}) — README 의 eslint 계약 절 모양으로 고친다",
                )
            ],
        )


def _eslint_problems(
    result: CheckResult, root: Path, section: Eslint, base_ref: str | None, eslint: EslintRunner
) -> list[Problem]:
    gated: list[Problem] = []
    contract, contract_text, problems = _load_contract(root, section)
    result.problems.extend(problems)
    exceptions_text = _read_optional(root, section.exceptions)
    exceptions = _parse_json(exceptions_text)
    if base_ref and exceptions_text is not None:
        base_exceptions = _parse_json(_show_text(root, base_ref, section.exceptions))
        if base_exceptions is not None:
            gated.extend(
                settings.eslint_exception_problems(section.exceptions, exceptions_text, base_exceptions, exceptions)
            )
    if base_ref and contract_text is not None:
        base_contract = _show_text(root, base_ref, section.contract)
        if base_contract is not None and _parse_json(base_contract) != _parse_json(contract_text):
            gated.append(
                Problem(
                    section.contract, 1, f"eslint 계약 파일이 기준 ref 와 다르다 — 되돌린다. {settings.gate_hint()}"
                )
            )
    if contract is None:
        return gated
    exception_map = exceptions if isinstance(exceptions, dict) else {}
    rules = contract.rules(exception_map)
    files = eslint_files(repo_files(root), section.dir, contract)
    if not files:
        return gated
    try:
        results = eslint(root, section.dir, files, rules, settings.eslint_probes(files, contract))
    except MetricError as error:
        result.problems.append(
            Problem(
                error.path,
                error.line,
                f"eslint 설정을 확인하지 못했다({error.message}) — {install_hint(section.dir)}",
            )
        )
        return gated
    gated.extend(settings.eslint_level_problems(section.dir, results.get("files", {}), exception_map, rules, contract))
    gated.extend(
        settings.eslint_contract_problems(
            settings.eslint_config_label(root, section.dir), results.get("probes", []), exception_map, contract
        )
    )
    return gated


def _settings(
    result: CheckResult, root: Path, config: Config, base_ref: str | None, is_approved: bool, eslint: EslintRunner
) -> None:
    gated: list[Problem] = []
    if (banned := config.get(BannedApi)) is not None:
        gated.extend(_banned_api_problems(root, banned, base_ref))
    if (linter := config.get(ImportLinter)) is not None:
        gated.extend(_import_linter_problems(root, linter, base_ref))
    if (ty := config.get(Ty)) is not None:
        gated.extend(_ty_setting_problems(root, ty, base_ref))
    if (section := config.get(Eslint)) is not None:
        gated.extend(_eslint_problems(result, root, section, base_ref, eslint))
    _gate(result, gated, is_approved)


def config_problems(base: Config, work: Config, text: str) -> list[Problem]:
    found: list[Problem] = []

    def add(needle: str, message: str) -> None:
        found.append(Problem(work.label, settings.line_of(text, needle), f"{message} {settings.gate_hint()}"))

    for pattern in work.exclude:
        if pattern not in base.exclude:
            add(
                f'"{pattern}"',
                f"주석 검사 제외 '{pattern}' 를 새로 더했다 — 지운다. 제외는 고칠 수 없는 파일에만 쓴다.",
            )
    for name in base.forbid_names:
        if name not in work.forbid_names:
            add("forbid_names", f"[repo] forbid_names 에서 '{name}' 를 뺐다 — 되돌린다.")
    for directory in base.tool_settings_dirs:
        if directory not in work.tool_settings_dirs:
            add("tool_settings_dirs", f"[repo] tool_settings_dirs 에서 '{directory or '.'}' 를 뺐다 — 되돌린다.")
    if base.baseline != work.baseline:
        add("baseline", "[trust] baseline 경로를 바꿨다 — 되돌린다.")
    if base.node_modules != work.node_modules:
        add("node_modules", "[trust] node_modules 경로를 바꿨다 — TS/JS 판정기가 바뀐다. 되돌린다.")
    for name in base.tables:
        if name not in work.tables:
            add("[metrics", f"[metrics.{name}] 절을 지워 지표를 껐다 — 되돌린다.")
    for name, table in work.tables.items():
        before = base.tables.get(name)
        if before is not None and before != table:
            add(f"[metrics.{name}]", f"[metrics.{name}] 설정이 기준 ref 와 다르다 — 되돌린다.")
    return found


def _compare_config(result: CheckResult, root: Path, config: Config, ref: str) -> tuple[Config | None, list[Problem]]:
    if config.relative is None:
        result.notices.append(f"알림: {config.label} 이 저장소 밖에 있어 기준 ref 설정과 비교하지 않는다")
        return None, []
    base_text = _show_text(root, ref, config.relative)
    work_text = _read_optional(root, config.relative)
    if base_text is None or work_text is None:
        result.notices.append(f"알림: 기준 ref 에 {config.label} 이 없어 설정 완화는 비교하지 않는다")
        return None, []
    try:
        base = config_module.parse(base_text, f"{ref}:{config.relative}", config.relative)
    except ConfigError as error:
        result.notices.append(f"알림: 기준 ref 의 설정을 읽지 못해 설정 완화는 비교하지 않는다({error})")
        return None, []
    return base, config_problems(base, config, work_text)


def _deleted_message(ref: str, effective: str, path: str, label: str) -> str:
    if effective == ref:
        where = f"기준 ref({ref})에 있던 {label}을 지웠다. git show {ref}:{path} 로 되살린다"
    else:
        where = f"지워진 {label}을 git show {effective}:{path} 로 되살린다"
    return f"{label} 파일이 없다 — {where}. {label} 삭제는 {LABEL} 표지로도 통과하지 않는다"


def _reference_values(
    result: CheckResult,
    config: Config,
    base: Mapping[str, int],
    present: set[str],
    worktree: Mapping[str, int],
    text: str,
) -> tuple[dict[str, int], list[Problem]]:
    keys = config.metric_keys()
    reference: dict[str, int] = {}
    gated: list[Problem] = []
    for key in keys:
        if key in base:
            reference[key] = base[key]
        elif key not in present and key in worktree:
            reference[key] = worktree[key]
            result.notices.append(
                f"알림: 기준 ref 기준선에 {key} 가 없다 — 새로 켠 지표로 보고 작업 트리 기준선 값 {worktree[key]} 과 비교한다"
            )
    for key in sorted(present - set(keys)):
        gated.append(
            Problem(
                config.baseline,
                _key_line(text, key),
                f"기준 ref 기준선에 있는 지표 {key} 가 지금 꺼져 있다 — {config.label} 의 [metrics.…] 절(또는 key)을 되돌린다. "
                f"{settings.gate_hint()}",
            )
        )
    return reference, gated


def check(
    root: Path,
    config: Config,
    ref: str,
    current: Mapping[str, int | None],
    is_approved: bool,
    eslint: EslintRunner = run_eslint,
) -> CheckResult:
    _verify_ref(root, ref)
    result = CheckResult()
    keys = config.metric_keys()
    path = config.baseline
    ty = config.get(Ty)
    work_text = _read_optional(root, path)
    worktree: dict[str, int] = {}
    if work_text is not None:
        worktree, problems = parse_baseline(work_text, path, keys)
        result.problems.extend(problems)
    base_config, config_gated = _compare_config(result, root, config, ref)
    _gate(result, config_gated, is_approved)
    base_path = base_config.baseline if base_config is not None else path
    base_text, effective = _baseline_source(root, ref, base_path)
    if base_text is None or effective is None:
        result.notices.append(f"알림: 기준 ref 에 {base_path} 이 없어 작업 트리 파일과 비교한다")
        if work_text is None:
            result.problems.append(Problem(path, 1, "기준선 파일이 없다 — metrics --update 로 만든다"))
        else:
            result.compared = worktree
            _compare_current(result, keys, current, worktree, path, work_text, is_approved)
        if ty is not None:
            _compare_ty(result, root, ty, ref, is_approved)
        _settings(result, root, config, None, is_approved, eslint)
        return result
    if effective != ref:
        result.notices.append(f"알림: 기준 ref({ref})에는 {base_path} 이 없다 — 지워진 기준선({effective})과 비교한다")
    if work_text is None:
        result.problems.append(Problem(path, 1, _deleted_message(ref, effective, base_path, "기준선")))
    base, present, problems = parse_values(base_text, f"{effective}:{base_path}")
    result.problems.extend(problems)
    reference, gated = _reference_values(result, config, base, present, worktree, base_text)
    result.compared = reference
    _compare_current(result, keys, current, reference, path, work_text or base_text, is_approved)
    _compare_baselines(result, keys, worktree, base, path, work_text or base_text, is_approved)
    if ty is not None:
        _compare_ty(result, root, ty, ref, is_approved)
    _gate(result, gated, is_approved)
    _settings(result, root, config, effective, is_approved, eslint)
    return result


def worktree_baseline(root: Path, config: Config) -> dict[str, int]:
    text = _read_optional(root, config.baseline)
    return {} if text is None else parse_values(text, config.baseline)[0]


def write_baseline(root: Path, config: Config, values: Mapping[str, int]) -> Path:
    path = root / config.baseline
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_baseline(values), encoding="utf-8")
    return path


def revert_ratio(root: Path) -> tuple[int, int]:
    subjects = gitfiles.git(root, "log", "--since=6.months", "--format=%s").decode("utf-8", "replace").splitlines()
    merges = gitfiles.git(root, "log", "--since=6.months", "--merges", "--format=%H").decode().split()
    return sum(1 for subject in subjects if REVERT_SUBJECT_RE.match(subject)), len(merges)


def metric_lines(keys: Sequence[str], current: Mapping[str, int | None], baseline: Mapping[str, int]) -> list[str]:
    lines = []
    for key in keys:
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
