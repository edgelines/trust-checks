from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path

from scripts.trust.checks import Problem

PYPROJECT_PATH = "backend/pyproject.toml"
ESLINT_EXCEPTIONS_PATH = "frontend/eslint-exceptions.json"
TID251 = "TID251"
ALLOWED_GLOB_DB_ENTRY = "tests/**"
GLOB_CHARS = frozenset("*?[")
AXIOS_RULE = "@typescript-eslint/no-restricted-imports"
FRONTEND_API_RULES = (
    "aox/no-react19-api",
    "no-restricted-syntax",
    "no-restricted-properties",
    AXIOS_RULE,
)
AXIOS_ALLOWED_FILES = frozenset({"src/utils/auth.ts", "src/utils/requestAuth.ts"})
REFERENCE_FILE = "src/index.tsx"
TY_LEVELS = {"ignore": 0, "warn": 1, "error": 2}
TY_DEFAULTS_PATH = Path(__file__).resolve().with_name("ty_rule_defaults.json")
UNKNOWN_TY_DEFAULT = "error"
SEVERITIES = {"off": 0, "warn": 1, "error": 2}
TID251_SPECIFICITY = {"ALL": 0, "TID": 2, "TID2": 4, "TID25": 5, "TID251": 7}
RUFF_KEYS = ("select", "extend-select", "ignore", "extend-ignore")
_TY_EXPLAIN_RE = re.compile(r"^# (?P<rule>[a-z0-9-]+)\s*\n+Default level: (?P<level>[a-z]+)\b", re.MULTILINE)
ESLINT_PROBE_PATH = "src/__trust_probe__/Probe.tsx"
ESLINT_PROBE_TEXT = (
    "import axios from 'axios';\n"
    "import { use, useActionState, useOptimistic } from 'react';\n"
    "import { useFormStatus, preload, preinit, preconnect, prefetchDNS } from 'react-dom';\n"
    "import * as ReactAll from 'react';\n"
    "export const names = [axios, use, useActionState, useOptimistic, useFormStatus, preload, preinit, preconnect, prefetchDNS];\n"
    "export const socket = new WebSocket('ws://probe');\n"
    "export const socketType = window.WebSocket;\n"
    "export const pending = Promise.withResolvers();\n"
    "export const viaNamespace = ReactAll.useOptimistic;\n"
)
ESLINT_CONTRACT = (
    ("axios", AXIOS_RULE, 1, 1),
    ("react 의 use·useActionState·useOptimistic", "aox/no-react19-api", 2, 3),
    ("react-dom 의 useFormStatus·preload·preinit·preconnect·prefetchDNS", "aox/no-react19-api", 3, 5),
    ("new WebSocket", "no-restricted-syntax", 6, 1),
    ("window.WebSocket", "no-restricted-syntax", 7, 1),
    ("Promise.withResolvers", "no-restricted-properties", 8, 1),
    ("react 의 useOptimistic(네임스페이스)", "aox/no-react19-api", 9, 1),
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


def covers_tid251(code: str) -> bool:
    return code.strip().upper() in TID251_SPECIFICITY


def _ruff_tables(data: Mapping) -> tuple[dict, dict]:
    ruff = _table(data, "tool", "ruff")
    return ruff, _table(ruff, "lint")


def _effective(lint: Mapping, ruff: Mapping, key: str) -> object:
    return lint[key] if key in lint else ruff.get(key)


def tid251_exceptions(data: Mapping) -> dict[str, list[str]]:
    ruff, lint = _ruff_tables(data)
    found: dict[str, list[str]] = {}
    for name in ("per-file-ignores", "extend-per-file-ignores"):
        table = _effective(lint, ruff, name)
        for pattern, codes in table.items() if isinstance(table, Mapping) else ():
            values = _strings(codes)
            if any(covers_tid251(code) for code in values):
                found.setdefault(pattern, []).extend(values)
    return found


def tid251_selection(data: Mapping) -> tuple[bool, tuple[str, str] | None]:
    ruff, lint = _ruff_tables(data)
    selects: list[int] = []
    ignores: list[tuple[int, str, str]] = []
    for key in RUFF_KEYS:
        for code in _strings(_effective(lint, ruff, key)):
            if not covers_tid251(code):
                continue
            specificity = TID251_SPECIFICITY[code.strip().upper()]
            if key.endswith("select"):
                selects.append(specificity)
            else:
                ignores.append((specificity, key, code))
    enabled = False
    culprit: tuple[str, str] | None = None
    for specificity in sorted({*selects, *(item[0] for item in ignores)}):
        if specificity in selects:
            enabled, culprit = True, None
        hits = [item for item in ignores if item[0] == specificity]
        if hits:
            enabled, culprit = False, (hits[0][1], hits[0][2])
    return enabled, culprit


def tid251_enabled(data: Mapping) -> bool:
    return tid251_selection(data)[0]


def has_per_file_ignores(data: Mapping) -> bool:
    ruff, lint = _ruff_tables(data)
    return any(isinstance(table.get("per-file-ignores"), Mapping) for table in (lint, ruff))


def _gate_hint() -> str:
    return "꼭 필요하면 PR 에 사람이 baseline-approved 표지를 단다(에이전트는 달지 않는다)"


def pyproject_absolute_problems(text: str, data: Mapping, root: Path) -> list[Problem]:
    problems: list[Problem] = []
    enabled, culprit = tid251_selection(data)
    if not enabled and culprit is not None:
        name, code = culprit
        problems.append(
            Problem(
                PYPROJECT_PATH,
                line_of(text, f'"{code}"'),
                f"ruff {name} 에 '{code}' 가 있어 TID251 이 꺼진다 — 지운다. {_gate_hint()}",
            )
        )
    elif not enabled:
        problems.append(
            Problem(
                PYPROJECT_PATH,
                line_of(text, "select"),
                "ruff 가 TID251 을 고르지 않는다 — [tool.ruff.lint] select 에 TID251 을 되돌린다"
                f"([tool.ruff.lint] 에 select 가 있으면 [tool.ruff] 의 select 는 무시된다). {_gate_hint()}",
            )
        )
    for pattern in sorted(tid251_exceptions(data)):
        if pattern == ALLOWED_GLOB_DB_ENTRY:
            continue
        line = line_of(text, f'"{pattern}"')
        if GLOB_CHARS & set(pattern):
            problems.append(
                Problem(
                    PYPROJECT_PATH,
                    line,
                    f"DB 직접 연결 예외 '{pattern}' 는 글롭이다 — 예외는 파일 경로 하나씩 적는다. {_gate_hint()}",
                )
            )
        elif not (root / "backend" / pattern).is_file():
            problems.append(
                Problem(
                    PYPROJECT_PATH,
                    line,
                    f"DB 직접 연결 예외 '{pattern}' 의 파일이 없다 — 예외를 지운다. {_gate_hint()}",
                )
            )
    return problems


def _contracts(data: Mapping) -> dict[str, Mapping]:
    contracts = _table(data, "tool", "importlinter").get("contracts", [])
    if not isinstance(contracts, list):
        return {}
    return {str(item.get("name")): item for item in contracts if isinstance(item, Mapping)}


def _without_ignores(contract: Mapping) -> dict:
    return {key: value for key, value in contract.items() if key != "ignore_imports"}


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


def pyproject_relative_problems(work_text: str, base: Mapping, work: Mapping) -> list[Problem]:
    problems: list[Problem] = []

    def add(needle: str, message: str) -> None:
        problems.append(Problem(PYPROJECT_PATH, line_of(work_text, needle), f"{message} {_gate_hint()}"))

    base_linter = {key: value for key, value in _table(base, "tool", "importlinter").items() if key != "contracts"}
    work_linter = {key: value for key, value in _table(work, "tool", "importlinter").items() if key != "contracts"}
    for key in sorted(set(base_linter) | set(work_linter)):
        if base_linter.get(key) != work_linter.get(key):
            add(key, f"import-linter 설정 '{key}' 가 바뀌었다 — 되돌린다.")
    base_contracts = _contracts(base)
    work_contracts = _contracts(work)
    for name, contract in base_contracts.items():
        mine = work_contracts.get(name)
        if mine is None:
            add("[[tool.importlinter.contracts]]", f"import-linter 계약 '{name}' 를 지웠다 — 되돌린다.")
        elif _without_ignores(mine) != _without_ignores(contract):
            add(f'"{name}"', f"import-linter 계약 '{name}' 의 설정이 바뀌었다 — 되돌린다.")
    for name, contract in work_contracts.items():
        before = set(_strings(base_contracts.get(name, {}).get("ignore_imports")))
        for pair in _strings(contract.get("ignore_imports")):
            if pair not in before:
                add(pair, f"import 예외 '{pair}' 를 새로 더했다 — 위반을 고친다.")
    before_keys = set(tid251_exceptions(base))
    for pattern in sorted(set(tid251_exceptions(work)) - before_keys):
        add(f'"{pattern}"', f"DB 직접 연결 예외 '{pattern}' 를 새로 더했다 — core.database 를 쓰게 고친다.")
    base_ruff, base_lint = _ruff_tables(base)
    work_ruff, work_lint = _ruff_tables(work)
    if base_ruff.get("extend") != work_ruff.get("extend"):
        add("extend", "ruff extend 를 바꿨다 — 다른 설정 파일을 끌어오지 않는다. 되돌린다.")
    banned_before = set(_table(base_lint, "flake8-tidy-imports", "banned-api"))
    banned_after = set(_table(work_lint, "flake8-tidy-imports", "banned-api"))
    for name in sorted(banned_before - banned_after):
        add("banned-api", f"금지 이름 '{name}' 를 뺐다 — 되돌린다.")
    for before_table, after_table in ((base_ruff, work_ruff), (base_lint, work_lint)):
        for key in ("exclude", "extend-exclude"):
            for value in sorted(set(_strings(after_table.get(key))) - set(_strings(before_table.get(key)))):
                add(f'"{value}"', f"ruff {key} 에 '{value}' 를 더했다 — 지운다.")
    base_ty = _table(base, "tool", "ty")
    work_ty = _table(work, "tool", "ty")
    for value in sorted(
        set(_strings(_table(work_ty, "src").get("exclude"))) - set(_strings(_table(base_ty, "src").get("exclude")))
    ):
        add(f'"{value}"', f"ty src.exclude 에 '{value}' 를 더했다 — 지운다.")
    if _table(work_ty, "src").get("include") != _table(base_ty, "src").get("include"):
        add("include", "ty src.include 를 바꿨다 — 되돌린다.")
    if work_ty.get("baseline") != base_ty.get("baseline"):
        add("baseline", "ty baseline 경로를 바꿨다 — 되돌린다.")
    for rule, before_label, after_label in _ty_global_loosening(base_ty, work_ty):
        add(f"{rule} =", f"ty 규칙 '{rule}' 수준을 {before_label} → {after_label} 으로 낮췄다 — 되돌린다.")
    for scope, rule, after_label, before_label in sorted(_ty_override_loosening(base, work)):
        add(
            f"{rule} =",
            f"ty overrides 에서 규칙 '{rule}' 를 {after_label} 으로 낮췄다(이전 {before_label}, 범위 {scope}) — 되돌린다.",
        )
    return problems


def eslint_pairs(data: object) -> set[tuple[str, str]]:
    if not isinstance(data, Mapping):
        return set()
    return {(rule, path) for rule, paths in data.items() for path in _strings(paths)}


def eslint_exception_problems(work_text: str, base: object, work: object) -> list[Problem]:
    return [
        Problem(
            ESLINT_EXCEPTIONS_PATH,
            line_of(work_text, f'"{path}"'),
            f"화면 예외 ({rule}, {path}) 를 새로 더했다 — 위반을 고친다. {_gate_hint()}",
        )
        for rule, path in sorted(eslint_pairs(work) - eslint_pairs(base))
    ]


def axios_exempt(file: str) -> bool:
    parts = file.split("/")
    name = parts[-1]
    return file in AXIOS_ALLOWED_FILES or "__tests__" in parts[1:-1] or ".test." in name


def _severity(value: object) -> int | None:
    level = value[0] if isinstance(value, list) and value else value
    if isinstance(level, str):
        return SEVERITIES.get(level)
    return level if isinstance(level, int) else None


def _options(value: object) -> list:
    return list(value[1:]) if isinstance(value, list) else []


def eslint_level_problems(
    results: Mapping[str, Mapping], exceptions: Mapping[str, Iterable[str]], rules: Iterable[str]
) -> list[Problem]:
    rules = list(rules)
    exempt_sets = {rule: set(exceptions.get(rule, [])) for rule in rules}
    reference = results.get(REFERENCE_FILE)
    reference_rules = reference.get("rules", {}) if reference and not reference.get("ignored") else {}
    problems: list[Problem] = []
    for file in sorted(results):
        entry = results[file]
        path = f"frontend/{file}"
        if entry.get("ignored"):
            problems.append(
                Problem(
                    path, 1, f"eslint 가 이 파일을 보지 않는다 — eslint.config.mjs 의 ignores 에서 뺀다. {_gate_hint()}"
                )
            )
            continue
        found = entry.get("rules", {})
        for rule in rules:
            if file in exempt_sets[rule] or (rule == AXIOS_RULE and axios_exempt(file)):
                continue
            value = found.get(rule)
            severity = _severity(value)
            if severity != SEVERITIES["error"]:
                problems.append(
                    Problem(
                        path,
                        1,
                        f"eslint 규칙 {rule} 가 error 가 아니다({severity}) — eslint.config.mjs 에서 error 로 되돌린다. "
                        f"{_gate_hint()}",
                    )
                )
            elif rule in reference_rules and _options(value) != _options(reference_rules[rule]):
                problems.append(
                    Problem(
                        path,
                        1,
                        f"eslint 규칙 {rule} 의 설정이 {REFERENCE_FILE} 와 다르다 — 파일별로 규칙 설정을 바꾸지 않는다. "
                        f"{_gate_hint()}",
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


def eslint_probes(files: Iterable[str]) -> list[dict[str, str]]:
    paths = [ESLINT_PROBE_PATH]
    if REFERENCE_FILE in set(files):
        paths.append(REFERENCE_FILE)
    return [{"filePath": path, "text": ESLINT_PROBE_TEXT} for path in paths]


def eslint_contract_problems(probes: Sequence[Mapping], exceptions: Mapping[str, Iterable[str]]) -> list[Problem]:
    problems: list[Problem] = []
    for probe in probes:
        path = str(probe.get("filePath"))
        if probe.get("ignored"):
            problems.append(
                Problem(
                    "frontend/eslint.config.mjs",
                    1,
                    f"eslint 가 검사용 파일 {path} 를 보지 않는다 — ignores 를 되돌린다. {_gate_hint()}",
                )
            )
            continue
        counts: dict[tuple[str, int], int] = {}
        for message in probe.get("messages", []):
            key = (str(message.get("ruleId")), int(message.get("line", 0)))
            counts[key] = counts.get(key, 0) + 1
        for label, rule, line, expected in ESLINT_CONTRACT:
            if path in set(exceptions.get(rule, [])) or (rule == AXIOS_RULE and axios_exempt(path)):
                continue
            if counts.get((rule, line), 0) < expected:
                problems.append(
                    Problem(
                        "frontend/eslint.config.mjs",
                        1,
                        f"eslint 금지 대상 '{label}' 가 막히지 않는다({path}, 규칙 {rule}) — "
                        f"금지 목록·옵션을 되돌린다. {_gate_hint()}",
                    )
                )
    return problems
