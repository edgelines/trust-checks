from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from trust_checks.checks import LABEL, Problem
from trust_checks.config import join

TY_LEVELS = {"ignore": 0, "warn": 1, "error": 2}
TY_DEFAULTS_PATH = Path(__file__).resolve().with_name("ty_rule_defaults.json")
UNKNOWN_TY_DEFAULT = "error"
SEVERITIES = {"off": 0, "warn": 1, "error": 2}
GLOB_CHARS = frozenset("*?[")
RUFF_KEYS = ("select", "extend-select", "ignore", "extend-ignore")
ESLINT_CONFIG_NAMES = (
    "eslint.config.mjs",
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
)
_TY_EXPLAIN_RE = re.compile(r"^# (?P<rule>[a-z0-9-]+)\s*\n+Default level: (?P<level>[a-z]+)\b", re.MULTILINE)
_LINTER_RE = re.compile(r"[A-Z]+")
PARENT_LINTERS = {"PLC": "PL", "PLE": "PL", "PLR": "PL", "PLW": "PL"}


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class Expectation:
    label: str
    rule: str
    line: int
    count: int


@dataclass(frozen=True)
class EslintContract:
    files: tuple[str, ...]
    reference: str
    exempt: dict[str, tuple[str, ...]]
    probe_path: str
    probe_text: str
    expect: tuple[Expectation, ...]

    def rules(self, exceptions: Mapping[str, object]) -> list[str]:
        return list(dict.fromkeys([*(item.rule for item in self.expect), *exceptions]))

    def exempt_file(self, rule: str, file: str) -> bool:
        pure = PurePosixPath(file)
        return any(pure.full_match(pattern) for pattern in self.exempt.get(rule, ()))

    def selects(self, file: str) -> bool:
        pure = PurePosixPath(file)
        return any(pure.full_match(pattern) for pattern in self.files)


def _text_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ContractError(f"{where} 는 비지 않은 문자열 목록이어야 한다")
    return tuple(value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where} 는 비지 않은 문자열이어야 한다")
    return value


def _positive(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{where} 는 1 이상 정수여야 한다")
    return value


def parse_contract(text: str) -> EslintContract:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(f"JSON 을 읽지 못했다: {error.msg}") from error
    if not isinstance(data, dict):
        raise ContractError("최상위가 객체가 아니다")
    unknown = sorted(set(data) - {"files", "reference", "exempt", "probe", "expect"})
    if unknown:
        raise ContractError(f"모르는 열쇠 {', '.join(unknown)}")
    exempt = data.get("exempt", {})
    if not isinstance(exempt, dict):
        raise ContractError("exempt 는 규칙 이름 → 글롭 목록 객체여야 한다")
    probe = data.get("probe")
    if not isinstance(probe, dict):
        raise ContractError("probe 는 {path, lines} 객체여야 한다")
    expect = data.get("expect")
    if not isinstance(expect, list) or not expect:
        raise ContractError("expect 는 비지 않은 목록이어야 한다")
    items: list[Expectation] = []
    for index, item in enumerate(expect):
        if not isinstance(item, dict):
            raise ContractError(f"expect[{index}] 는 객체여야 한다")
        items.append(
            Expectation(
                _text(item.get("label"), f"expect[{index}].label"),
                _text(item.get("rule"), f"expect[{index}].rule"),
                _positive(item.get("line"), f"expect[{index}].line"),
                _positive(item.get("count"), f"expect[{index}].count"),
            )
        )
    lines = _text_list(probe.get("lines"), "probe.lines")
    return EslintContract(
        files=_text_list(data.get("files"), "files"),
        reference=_text(data.get("reference"), "reference"),
        exempt={rule: _text_list(globs, f"exempt.{rule}") for rule, globs in exempt.items()},
        probe_path=_text(probe.get("path"), "probe.path"),
        probe_text="\n".join(lines) + "\n",
        expect=tuple(items),
    )


def _table(data: Mapping, *keys: str) -> dict:
    current: object = data
    for key in keys:
        current = current.get(key, {}) if isinstance(current, Mapping) else {}
    return dict(current) if isinstance(current, Mapping) else {}


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.split("\n"), start=1):
        if needle in line:
            return number
    return 1


def code_prefixes(code: str) -> dict[str, int]:
    target = code.strip().upper()
    match = _LINTER_RE.match(target)
    start = match.end() if match else len(target)
    parent = PARENT_LINTERS.get(target[:start])
    found = {"ALL": 0, **({parent: len(parent)} if parent else {})}
    return {**found, **{target[:end]: end for end in range(max(start, 1), len(target) + 1)}}


def covers(value: str, code: str) -> bool:
    return value.strip().upper() in code_prefixes(code)


def _ruff_tables(data: Mapping) -> tuple[dict, dict]:
    ruff = _table(data, "tool", "ruff")
    return ruff, _table(ruff, "lint")


def _effective(lint: Mapping, ruff: Mapping, key: str) -> object:
    return lint[key] if key in lint else ruff.get(key)


def code_exceptions(data: Mapping, code: str) -> dict[str, list[str]]:
    ruff, lint = _ruff_tables(data)
    found: dict[str, list[str]] = {}
    for name in ("per-file-ignores", "extend-per-file-ignores"):
        table = _effective(lint, ruff, name)
        for pattern, codes in table.items() if isinstance(table, Mapping) else ():
            values = _strings(codes)
            if any(covers(value, code) for value in values):
                found.setdefault(pattern, []).extend(values)
    return found


def code_selection(data: Mapping, code: str) -> tuple[bool, tuple[str, str] | None]:
    ruff, lint = _ruff_tables(data)
    specificity = code_prefixes(code)
    selects: list[int] = []
    ignores: list[tuple[int, str, str]] = []
    for key in RUFF_KEYS:
        for value in _strings(_effective(lint, ruff, key)):
            if not covers(value, code):
                continue
            level = specificity[value.strip().upper()]
            if key.endswith("select"):
                selects.append(level)
            else:
                ignores.append((level, key, value))
    enabled = False
    culprit: tuple[str, str] | None = None
    for level in sorted({*selects, *(item[0] for item in ignores)}):
        if level in selects:
            enabled, culprit = True, None
        hits = [item for item in ignores if item[0] == level]
        if hits:
            enabled, culprit = False, (hits[0][1], hits[0][2])
    return enabled, culprit


def code_enabled(data: Mapping, code: str) -> bool:
    return code_selection(data, code)[0]


def has_per_file_ignores(data: Mapping) -> bool:
    ruff, lint = _ruff_tables(data)
    return any(isinstance(table.get("per-file-ignores"), Mapping) for table in (lint, ruff))


def gate_hint() -> str:
    return f"꼭 필요하면 PR 에 사람이 {LABEL} 표지를 단다(에이전트는 달지 않는다)"


def banned_api_absolute_problems(
    path: str, text: str, data: Mapping, project_dir: Path, code: str, allow: Iterable[str]
) -> list[Problem]:
    problems: list[Problem] = []
    allowed = set(allow)
    enabled, culprit = code_selection(data, code)
    if not enabled and culprit is not None:
        name, value = culprit
        problems.append(
            Problem(
                path,
                line_of(text, f'"{value}"'),
                f"ruff {name} 에 '{value}' 가 있어 {code} 이 꺼진다 — 지운다. {gate_hint()}",
            )
        )
    elif not enabled:
        problems.append(
            Problem(
                path,
                line_of(text, "select"),
                f"ruff 가 {code} 을 고르지 않는다 — [tool.ruff.lint] select 에 {code} 을 되돌린다"
                f"([tool.ruff.lint] 에 select 가 있으면 [tool.ruff] 의 select 는 무시된다). {gate_hint()}",
            )
        )
    for pattern in sorted(code_exceptions(data, code)):
        if pattern in allowed:
            continue
        line = line_of(text, f'"{pattern}"')
        if GLOB_CHARS & set(pattern):
            problems.append(
                Problem(
                    path,
                    line,
                    f"금지 이름({code}) 예외 '{pattern}' 는 글롭이다 — 예외는 파일 경로 하나씩 적는다. {gate_hint()}",
                )
            )
        elif not (project_dir / pattern).is_file():
            problems.append(
                Problem(path, line, f"금지 이름({code}) 예외 '{pattern}' 의 파일이 없다 — 예외를 지운다. {gate_hint()}")
            )
    return problems


class _Collector:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.problems: list[Problem] = []

    def add(self, needle: str, message: str) -> None:
        self.problems.append(Problem(self.path, line_of(self.text, needle), f"{message} {gate_hint()}"))


def banned_api_relative_problems(path: str, work_text: str, base: Mapping, work: Mapping, code: str) -> list[Problem]:
    found = _Collector(path, work_text)
    for pattern in sorted(set(code_exceptions(work, code)) - set(code_exceptions(base, code))):
        found.add(
            f'"{pattern}"',
            f"금지 이름({code}) 예외 '{pattern}' 를 새로 더했다 — 금지 이름 대신 허용된 창구를 쓰게 고친다.",
        )
    base_ruff, base_lint = _ruff_tables(base)
    work_ruff, work_lint = _ruff_tables(work)
    if base_ruff.get("extend") != work_ruff.get("extend"):
        found.add("extend", "ruff extend 를 바꿨다 — 다른 설정 파일을 끌어오지 않는다. 되돌린다.")
    banned_before = set(_table(base_lint, "flake8-tidy-imports", "banned-api"))
    banned_after = set(_table(work_lint, "flake8-tidy-imports", "banned-api"))
    for name in sorted(banned_before - banned_after):
        found.add("banned-api", f"금지 이름 '{name}' 를 뺐다 — 되돌린다.")
    for before_table, after_table in ((base_ruff, work_ruff), (base_lint, work_lint)):
        for key in ("exclude", "extend-exclude"):
            for value in sorted(set(_strings(after_table.get(key))) - set(_strings(before_table.get(key)))):
                found.add(f'"{value}"', f"ruff {key} 에 '{value}' 를 더했다 — 지운다.")
    return found.problems


def _contracts(data: Mapping) -> dict[str, Mapping]:
    contracts = _table(data, "tool", "importlinter").get("contracts", [])
    if not isinstance(contracts, list):
        return {}
    return {str(item.get("name")): item for item in contracts if isinstance(item, Mapping)}


def _without_ignores(contract: Mapping) -> dict:
    return {key: value for key, value in contract.items() if key != "ignore_imports"}


def import_linter_relative_problems(path: str, work_text: str, base: Mapping, work: Mapping) -> list[Problem]:
    found = _Collector(path, work_text)
    base_linter = {key: value for key, value in _table(base, "tool", "importlinter").items() if key != "contracts"}
    work_linter = {key: value for key, value in _table(work, "tool", "importlinter").items() if key != "contracts"}
    for key in sorted(set(base_linter) | set(work_linter)):
        if base_linter.get(key) != work_linter.get(key):
            found.add(key, f"import-linter 설정 '{key}' 가 바뀌었다 — 되돌린다.")
    base_contracts = _contracts(base)
    work_contracts = _contracts(work)
    for name, contract in base_contracts.items():
        mine = work_contracts.get(name)
        if mine is None:
            found.add("[[tool.importlinter.contracts]]", f"import-linter 계약 '{name}' 를 지웠다 — 되돌린다.")
        elif _without_ignores(mine) != _without_ignores(contract):
            found.add(f'"{name}"', f"import-linter 계약 '{name}' 의 설정이 바뀌었다 — 되돌린다.")
    for name, contract in work_contracts.items():
        before = set(_strings(base_contracts.get(name, {}).get("ignore_imports")))
        for pair in _strings(contract.get("ignore_imports")):
            if pair not in before:
                found.add(pair, f"import 예외 '{pair}' 를 새로 더했다 — 위반을 고친다.")
    return found.problems


def _ty_rules(table: Mapping) -> dict[str, str]:
    return {name: level for name, level in _table(table, "rules").items() if isinstance(level, str)}


def _override_key(override: Mapping) -> str:
    return json.dumps({key: override.get(key) for key in ("include", "exclude")}, sort_keys=True, ensure_ascii=False)


def _ty_rank(level: str) -> int:
    return TY_LEVELS.get(level, -1)


def _global_level(ty_table: Mapping, rule: str) -> tuple[str, str]:
    explicit = _ty_rules(ty_table).get(rule)
    if explicit is not None:
        return explicit, explicit
    default = ty_default_level(rule)
    return default, f"기본({default})"


def _override_levels(data: Mapping) -> dict[tuple[str, str], str]:
    overrides = _table(data, "tool", "ty").get("overrides", [])
    levels: dict[tuple[str, str], str] = {}
    for override in overrides if isinstance(overrides, list) else []:
        if isinstance(override, Mapping):
            for rule, level in _ty_rules(override).items():
                levels[(_override_key(override), rule)] = level
    return levels


def _scope_level(data: Mapping, levels: Mapping[tuple[str, str], str], scope: str, rule: str) -> tuple[str, str]:
    level = levels.get((scope, rule))
    if level is not None:
        return level, level
    return _global_level(_table(data, "tool", "ty"), rule)


def _ty_override_loosening(base: Mapping, work: Mapping) -> set[tuple[str, str, str, str]]:
    base_levels = _override_levels(base)
    work_levels = _override_levels(work)
    found: set[tuple[str, str, str, str]] = set()
    for scope, rule in set(base_levels) | set(work_levels):
        before, before_label = _scope_level(base, base_levels, scope, rule)
        after, after_label = _scope_level(work, work_levels, scope, rule)
        if _ty_rank(after) < _ty_rank(before):
            found.add((scope, rule, after_label, before_label))
    return found


def _ty_global_loosening(base_ty: Mapping, work_ty: Mapping) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for rule in sorted(set(_ty_rules(base_ty)) | set(_ty_rules(work_ty))):
        before, before_label = _global_level(base_ty, rule)
        after, after_label = _global_level(work_ty, rule)
        if _ty_rank(after) < _ty_rank(before):
            found.append((rule, before_label, after_label))
    return found


def ty_relative_problems(path: str, work_text: str, base: Mapping, work: Mapping) -> list[Problem]:
    found = _Collector(path, work_text)
    base_ty = _table(base, "tool", "ty")
    work_ty = _table(work, "tool", "ty")
    for value in sorted(
        set(_strings(_table(work_ty, "src").get("exclude"))) - set(_strings(_table(base_ty, "src").get("exclude")))
    ):
        found.add(f'"{value}"', f"ty src.exclude 에 '{value}' 를 더했다 — 지운다.")
    if _table(work_ty, "src").get("include") != _table(base_ty, "src").get("include"):
        found.add("include", "ty src.include 를 바꿨다 — 되돌린다.")
    if work_ty.get("baseline") != base_ty.get("baseline"):
        found.add("baseline", "ty baseline 경로를 바꿨다 — 되돌린다.")
    for rule, before_label, after_label in _ty_global_loosening(base_ty, work_ty):
        found.add(f"{rule} =", f"ty 규칙 '{rule}' 수준을 {before_label} → {after_label} 으로 낮췄다 — 되돌린다.")
    for scope, rule, after_label, before_label in sorted(_ty_override_loosening(base, work)):
        found.add(
            f"{rule} =",
            f"ty overrides 에서 규칙 '{rule}' 를 {after_label} 으로 낮췄다(이전 {before_label}, 범위 {scope}) — 되돌린다.",
        )
    return found.problems


def eslint_pairs(data: object) -> set[tuple[str, str]]:
    if not isinstance(data, Mapping):
        return set()
    return {(rule, path) for rule, paths in data.items() for path in _strings(paths)}


def eslint_exception_problems(path: str, work_text: str, base: object, work: object) -> list[Problem]:
    return [
        Problem(
            path,
            line_of(work_text, f'"{file}"'),
            f"eslint 예외 ({rule}, {file}) 를 새로 더했다 — 위반을 고친다. {gate_hint()}",
        )
        for rule, file in sorted(eslint_pairs(work) - eslint_pairs(base))
    ]


def _severity(value: object) -> int | None:
    level = value[0] if isinstance(value, list) and value else value
    if isinstance(level, str):
        return SEVERITIES.get(level)
    return level if isinstance(level, int) else None


def _options(value: object) -> list:
    return list(value[1:]) if isinstance(value, list) else []


def eslint_config_label(root: Path, directory: str) -> str:
    for name in ESLINT_CONFIG_NAMES:
        if (root / directory / name).is_file():
            return join(directory, name)
    return join(directory, ESLINT_CONFIG_NAMES[0])


def eslint_level_problems(
    directory: str,
    results: Mapping[str, Mapping],
    exceptions: Mapping[str, Iterable[str]],
    rules: Iterable[str],
    contract: EslintContract,
) -> list[Problem]:
    rules = list(rules)
    exempt_sets = {rule: set(exceptions.get(rule, [])) for rule in rules}
    reference = results.get(contract.reference)
    reference_rules = reference.get("rules", {}) if reference and not reference.get("ignored") else {}
    problems: list[Problem] = []
    for file in sorted(results):
        entry = results[file]
        path = join(directory, file)
        if entry.get("ignored"):
            problems.append(
                Problem(path, 1, f"eslint 가 이 파일을 보지 않는다 — eslint 설정의 ignores 에서 뺀다. {gate_hint()}")
            )
            continue
        found = entry.get("rules", {})
        for rule in rules:
            if file in exempt_sets[rule] or contract.exempt_file(rule, file):
                continue
            value = found.get(rule)
            severity = _severity(value)
            if severity != SEVERITIES["error"]:
                problems.append(
                    Problem(
                        path,
                        1,
                        f"eslint 규칙 {rule} 가 error 가 아니다({severity}) — eslint 설정에서 error 로 되돌린다. "
                        f"{gate_hint()}",
                    )
                )
            elif rule in reference_rules and _options(value) != _options(reference_rules[rule]):
                problems.append(
                    Problem(
                        path,
                        1,
                        f"eslint 규칙 {rule} 의 설정이 {contract.reference} 와 다르다 — 파일별로 규칙 설정을 바꾸지 않는다. "
                        f"{gate_hint()}",
                    )
                )
    return problems


def eslint_probes(files: Iterable[str], contract: EslintContract) -> list[dict[str, str]]:
    paths = [contract.probe_path]
    if contract.reference in set(files):
        paths.append(contract.reference)
    return [{"filePath": path, "text": contract.probe_text} for path in paths]


def eslint_contract_problems(
    config_label: str, probes: Sequence[Mapping], exceptions: Mapping[str, Iterable[str]], contract: EslintContract
) -> list[Problem]:
    problems: list[Problem] = []
    for probe in probes:
        path = str(probe.get("filePath"))
        if probe.get("ignored"):
            problems.append(
                Problem(
                    config_label, 1, f"eslint 가 검사용 파일 {path} 를 보지 않는다 — ignores 를 되돌린다. {gate_hint()}"
                )
            )
            continue
        counts: dict[tuple[str, int], int] = {}
        for message in probe.get("messages", []):
            key = (str(message.get("ruleId")), int(message.get("line", 0)))
            counts[key] = counts.get(key, 0) + 1
        for item in contract.expect:
            if path in set(exceptions.get(item.rule, [])) or contract.exempt_file(item.rule, path):
                continue
            if counts.get((item.rule, item.line), 0) < item.count:
                problems.append(
                    Problem(
                        config_label,
                        1,
                        f"eslint 금지 대상 '{item.label}' 가 막히지 않는다({path}, 규칙 {item.rule}) — "
                        f"금지 목록·옵션을 되돌린다. {gate_hint()}",
                    )
                )
    return problems


@cache
def load_ty_defaults() -> dict:
    return json.loads(TY_DEFAULTS_PATH.read_text(encoding="utf-8"))


def ty_default_level(rule: str) -> str:
    level = load_ty_defaults()["rules"].get(rule, UNKNOWN_TY_DEFAULT)
    return level if level in TY_LEVELS else UNKNOWN_TY_DEFAULT


def parse_ty_explain(text: str) -> dict[str, str]:
    return {match.group("rule"): match.group("level") for match in _TY_EXPLAIN_RE.finditer(text)}


def render_ty_defaults(version: str, explain: str) -> str:
    rules = dict(sorted(parse_ty_explain(explain).items()))
    return json.dumps({"version": version, "rules": rules}, ensure_ascii=False, indent=2) + "\n"
