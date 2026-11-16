from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_BASELINE = "trust-baseline.json"
CONFIG_NAME = "trust.toml"
COMMENT_KEYS = (
    "suppressions",
    "allowed_comments_external",
    "allowed_comments_license",
    "allowed_comments_issue",
    "disallowed_comments",
)
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
_REQUIRED = object()


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ImportLinter:
    key: str
    pyproject: str
    contract: str


@dataclass(frozen=True)
class BannedApi:
    key: str
    pyproject: str
    code: str
    allow: tuple[str, ...]


@dataclass(frozen=True)
class Eslint:
    key: str
    dir: str
    exceptions: str
    contract: str


@dataclass(frozen=True)
class TsAny:
    key: str
    roots: tuple[str, ...]


@dataclass(frozen=True)
class Ty:
    key: str
    pyproject: str
    baseline: str


@dataclass(frozen=True)
class Knip:
    key: str
    dir: str


@dataclass(frozen=True)
class Quarantine:
    key: str
    tests: str
    marker: str


@dataclass(frozen=True)
class ProseOnly:
    key: str
    doc: str
    heading: str


Section = ImportLinter | BannedApi | Eslint | TsAny | Ty | Knip | Quarantine | ProseOnly

_FieldSpec = tuple[str, str, object]
METRIC_SECTIONS: dict[str, tuple[str, type, tuple[_FieldSpec, ...]]] = {
    "import_linter": (
        "import_linter_exceptions",
        ImportLinter,
        (("pyproject", "path", _REQUIRED), ("contract", "str", _REQUIRED)),
    ),
    "banned_api": (
        "banned_api_exceptions",
        BannedApi,
        (("pyproject", "path", _REQUIRED), ("code", "str", _REQUIRED), ("allow", "list", ())),
    ),
    "eslint": (
        "eslint_exceptions",
        Eslint,
        (("dir", "dir", _REQUIRED), ("exceptions", "path", _REQUIRED), ("contract", "path", _REQUIRED)),
    ),
    "ts_any": ("ts_any", TsAny, (("roots", "dirs", _REQUIRED),)),
    "ty": ("ty_baseline_entries", Ty, (("pyproject", "path", _REQUIRED), ("baseline", "path", _REQUIRED))),
    "knip": ("knip_unused", Knip, (("dir", "dir", _REQUIRED),)),
    "ci_quarantine": ("ci_quarantine", Quarantine, (("tests", "dir", _REQUIRED), ("marker", "str", "ci_quarantine"))),
    "prose_only_rules": (
        "prose_only_rules",
        ProseOnly,
        (("doc", "path", _REQUIRED), ("heading", "str", "규칙과 강제 수단")),
    ),
}
_KEY_ORDER = ("import_linter", "banned_api", "eslint", "ts_any", "", "ty", "knip", "ci_quarantine", "prose_only_rules")
_TOP = {
    "trust": ("baseline", "node_modules"),
    "comments": ("exclude",),
    "repo": ("forbid_names", "tool_settings_dirs"),
    "metrics": tuple(METRIC_SECTIONS),
}


@dataclass(frozen=True)
class Config:
    label: str
    relative: str | None = None
    baseline: str = DEFAULT_BASELINE
    node_modules: str | None = None
    exclude: tuple[str, ...] = ()
    forbid_names: tuple[str, ...] = ()
    tool_settings_dirs: tuple[str, ...] = ()
    sections: dict[str, Section] = field(default_factory=dict)
    tables: dict[str, dict] = field(default_factory=dict)

    def get[S](self, kind: type[S]) -> S | None:
        for value in self.sections.values():
            if isinstance(value, kind):
                return value
        return None

    def metric_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        for name in _KEY_ORDER:
            if not name:
                keys.extend(COMMENT_KEYS)
            elif name in self.sections:
                keys.append(self.sections[name].key)
        return tuple(keys)

    def section_of(self, key: str) -> str | None:
        return next((name for name, value in self.sections.items() if value.key == key), None)

    def ts_lib(self, root: Path) -> Path | None:
        if self.node_modules is None:
            return None
        return root / self.node_modules / "typescript/lib/typescript.js"


def norm_dir(value: str) -> str:
    text = value.strip().rstrip("/").removeprefix("./")
    return "" if text in ("", ".") else PurePosixPath(text).as_posix()


def under(path: str, directory: str) -> bool:
    return not directory or path == directory or path.startswith(f"{directory}/")


def join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


class _Reader:
    def __init__(self, label: str) -> None:
        self.label = label

    def fail(self, message: str) -> ConfigError:
        return ConfigError(f"{self.label}: {message}")

    def table(self, data: Mapping, name: str, where: str) -> dict:
        value = data.get(name, {})
        if not isinstance(value, Mapping):
            raise self.fail(f"{where}{name} 는 표여야 한다 — [{where}{name}] 절로 쓴다")
        return dict(value)

    def only(self, table: Mapping, allowed: tuple[str, ...], where: str) -> None:
        for key in table:
            if key not in allowed:
                hint = ", ".join(allowed) if allowed else "없음"
                raise self.fail(f"{where} 에 모르는 열쇠 '{key}' 가 있다 — 지운다. 쓸 수 있는 열쇠: {hint}")

    def string(self, table: Mapping, name: str, where: str, default: object) -> object:
        if name not in table:
            if default is _REQUIRED:
                raise self.fail(f'{where} 에 {name} 이 없다 — {name} = "…" 를 적는다(README 의 trust.toml 절)')
            return default
        value = table[name]
        if not isinstance(value, str) or not value.strip():
            raise self.fail(f'{where} 의 {name} 은 비지 않은 문자열이어야 한다 — {name} = "…" 로 고친다')
        return value.strip()

    def path(self, value: str, name: str, where: str, absolute: bool = False) -> str:
        pure = PurePosixPath(value)
        if pure.is_absolute() and not absolute:
            raise self.fail(f"{where} 의 {name} '{value}' 는 절대 경로다 — 저장소 뿌리 기준 상대 경로로 쓴다")
        if ".." in pure.parts:
            raise self.fail(f"{where} 의 {name} '{value}' 에 '..' 가 있다 — 저장소 안의 경로를 뿌리 기준으로 쓴다")
        return value

    def strings(self, table: Mapping, name: str, where: str, default: object) -> tuple[str, ...]:
        if name not in table:
            if default is _REQUIRED:
                raise self.fail(f'{where} 에 {name} 이 없다 — {name} = ["…"] 를 적는다(README 의 trust.toml 절)')
            return tuple(default) if isinstance(default, tuple) else ()
        value = table[name]
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise self.fail(f'{where} 의 {name} 은 비지 않은 문자열 목록이어야 한다 — {name} = ["…"] 로 고친다')
        if default is _REQUIRED and not value:
            raise self.fail(f"{where} 의 {name} 이 비었다 — 하나 이상 적거나 절을 지운다")
        return tuple(item.strip() for item in value)


def _section(reader: _Reader, name: str, table: dict) -> Section:
    default_key, kind, fields = METRIC_SECTIONS[name]
    where = f"[metrics.{name}]"
    reader.only(table, (*(spec[0] for spec in fields), "key"), where)
    key = reader.string(table, "key", where, default_key)
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise reader.fail(
            f"{where} 의 key '{key}' 는 쓸 수 없는 이름이다 — 영문 소문자·숫자·밑줄로 쓴다(첫 글자는 영문)"
        )
    values: dict[str, object] = {"key": key}
    for field_name, field_kind, default in fields:
        if field_kind == "dirs":
            items = reader.strings(table, field_name, where, default)
            values[field_name] = tuple(norm_dir(reader.path(item, field_name, where)) for item in items)
        elif field_kind == "list":
            values[field_name] = reader.strings(table, field_name, where, default)
        else:
            value = str(reader.string(table, field_name, where, default))
            if field_kind == "dir":
                value = norm_dir(reader.path(value, field_name, where))
            elif field_kind == "path":
                value = reader.path(value, field_name, where).removeprefix("./")
            values[field_name] = value
    return kind(**values)


def parse(text: str, label: str, relative: str | None = None) -> Config:
    reader = _Reader(label)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise reader.fail(f"TOML 을 읽지 못했다({error}) — 문법을 고친다") from error
    reader.only(data, tuple(_TOP), "최상위")
    tables = {name: reader.table(data, name, "") for name in _TOP}
    for name, allowed in _TOP.items():
        reader.only(tables[name], allowed, f"[{name}]")
    trust, comments, repo, metrics = (tables[name] for name in ("trust", "comments", "repo", "metrics"))
    sections: dict[str, Section] = {}
    raw: dict[str, dict] = {}
    owners: dict[str, str] = {key: "주석 기본 지표" for key in COMMENT_KEYS}
    for name in METRIC_SECTIONS:
        if name not in metrics:
            continue
        table = reader.table(metrics, name, "metrics.")
        section = _section(reader, name, table)
        if section.key in owners:
            raise reader.fail(
                f"[metrics.{name}] 의 열쇠 이름 '{section.key}' 가 {owners[section.key]} 과 겹친다 — key 를 다르게 쓴다"
            )
        owners[section.key] = f"[metrics.{name}]"
        sections[name] = section
        raw[name] = {key: value for key, value in table.items() if key != "key"}
    node_modules = reader.string(trust, "node_modules", "[trust]", None)
    if isinstance(node_modules, str):
        node_modules = norm_dir(reader.path(node_modules, "node_modules", "[trust]", absolute=True))
    baseline = reader.path(str(reader.string(trust, "baseline", "[trust]", DEFAULT_BASELINE)), "baseline", "[trust]")
    directories = reader.strings(repo, "tool_settings_dirs", "[repo]", ())
    return Config(
        label=label,
        relative=relative,
        baseline=baseline.removeprefix("./"),
        node_modules=node_modules if isinstance(node_modules, str) else None,
        exclude=reader.strings(comments, "exclude", "[comments]", ()),
        forbid_names=reader.strings(repo, "forbid_names", "[repo]", ()),
        tool_settings_dirs=tuple(norm_dir(reader.path(item, "tool_settings_dirs", "[repo]")) for item in directories),
        sections=sections,
        tables=raw,
    )


def load(path: Path, label: str, relative: str | None = None) -> Config:
    if not path.is_file():
        raise ConfigError(
            f"{label} 가 없다 — 저장소 뿌리에 {CONFIG_NAME} 을 두거나 --config 로 경로를 준다(모양은 README 의 trust.toml 절)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{label} 를 UTF-8 로 읽지 못했다 — UTF-8 로 저장한다") from None
    return parse(text, label, relative)
