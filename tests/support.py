import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import pytest

from trust_checks import checks, cli, metrics, scan, settings
from trust_checks.config import Config, load, parse
from trust_checks.scan import FileScan

REPO = Path(__file__).resolve().parents[1]
NODE_MODULES = REPO / "node_modules"
TS_LIB = NODE_MODULES / "typescript/lib/typescript.js"
ESLINT_FIXTURE = REPO / "tests/fixtures/eslint"
TODAY = date(2026, 11, 16)
TODAY_ENV = {"TRUST_TODAY": "2026-11-16"}
APPROVED_ENV = {**TODAY_ENV, "TRUST_BASELINE_APPROVED": "1"}
GATES = "gen"
BASELINE_PATH = "trust-baseline.json"
TY_BASELINE_PATH = "app/ty-baseline.json"
PYPROJECT_PATH = "app/pyproject.toml"
DB_ALLOW = ("core/database.py", "tests/**")
API_RULES = [
    "app/no-react19-api",
    "no-restricted-syntax",
    "no-restricted-properties",
    "@typescript-eslint/no-restricted-imports",
]

MOVED_LINES = 12
MOVED_BODY = "".join(f"value_{index} = {index}\n" for index in range(MOVED_LINES))

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=GIT_ENV, check=True)
    return result.stdout


def write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def init_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", os.devnull)
    return root


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def replace_in(root: Path, path: str, old: str, new: str) -> None:
    text = (root / path).read_text(encoding="utf-8")
    assert old in text
    write(root, path, text.replace(old, new))


def scan_text(path: str, text: str) -> FileScan:
    return scan.scan_text(path, text, TS_LIB)


def lines_of(path: str, text: str) -> list[int]:
    return [comment.line for comment in scan_text(path, text).comments]


def categories(path: str, text: str) -> list[str]:
    return [comment.category for comment in scan_text(path, text).comments]


def problems(path: str, text: str, base: str | None = None, is_approved: bool = False) -> list[str]:
    current = scan_text(path, text)
    previous = scan_text(path, base) if base is not None else None
    return [problem.render() for problem in checks.judge_scan(current, previous, TODAY, is_approved)]


COMMENTS_TOML = (
    "[trust]\n"
    f'node_modules = "{NODE_MODULES}"\n'
    "\n[comments]\n"
    f'exclude = ["{GATES}/**"]\n'
    "\n[repo]\n"
    'forbid_names = ["CLAUDE.md"]\n'
    'tool_settings_dirs = ["app"]\n'
)

METRICS_TOML = (
    "[trust]\n"
    f'baseline = "{BASELINE_PATH}"\n'
    f'node_modules = "{NODE_MODULES}"\n'
    "\n[metrics.import_linter]\n"
    f'pyproject = "{PYPROJECT_PATH}"\n'
    'contract = "services"\n'
    "\n[metrics.banned_api]\n"
    f'pyproject = "{PYPROJECT_PATH}"\n'
    'code = "TID251"\n'
    'allow = ["core/database.py", "tests/**"]\n'
    "\n[metrics.eslint]\n"
    'dir = "web"\n'
    'exceptions = "web/eslint-exceptions.json"\n'
    'contract = "web/eslint-contract.json"\n'
    "\n[metrics.ts_any]\n"
    'roots = ["web/src"]\n'
    "\n[metrics.ty]\n"
    f'pyproject = "{PYPROJECT_PATH}"\n'
    f'baseline = "{TY_BASELINE_PATH}"\n'
    "\n[metrics.knip]\n"
    'dir = "web"\n'
    "\n[metrics.ci_quarantine]\n"
    'tests = "app/tests"\n'
    "\n[metrics.prose_only_rules]\n"
    'doc = "docs/rules.md"\n'
)

PYPROJECT = """[tool.importlinter]
root_packages = ["core", "src"]

[[tool.importlinter.contracts]]
name = "core"
type = "forbidden"
source_modules = ["core"]
forbidden_modules = ["src"]

[[tool.importlinter.contracts]]
name = "services"
type = "forbidden"
source_modules = ["src.services"]
forbidden_modules = ["src.routes"]
ignore_imports = [
    "src.services.a -> src.routes.b",
    "src.services.c -> src.routes.d",
]

[tool.ruff.lint]
select = ["F", "TID251"]

[tool.ruff.lint.per-file-ignores]
"core/database.py" = ["TID251"]
"tests/**" = ["TID251"]
"src/x.py" = ["TID251"]
"src/y.py" = ["F401", "TID251"]
"main.py" = ["F401"]
"""

STANDARDS = """# 규칙

## 규칙과 강제 수단

설명.

| 규칙 | 출처 | 강제 수단 | 상태 |
|---|---|---|---|
| 가 | AGENTS.md | lint-imports | 강제됨 |
| 나 `a | b` | AGENTS.md | 없음 | 문서뿐 |
| 다 | AGENTS.md | 코드 리뷰 | 판단(사람) |
| 라 | AGENTS.md | 없음 | 문서뿐 |

## 다음 절

| 규칙 | 출처 | 강제 수단 | 상태 |
|---|---|---|---|
| 마 | AGENTS.md | 없음 | 문서뿐 |
"""

TY_BASELINE = {
    "version": 0,
    "files": {
        "a.py": [{"rule": "x", "startColumn": 1, "endColumn": 2, "lineCount": 1}],
        "b.py": [{"rule": "y", "startColumn": 1, "endColumn": 2, "lineCount": 1}],
    },
}

KNIP_OUTPUT = json.dumps(
    {
        "issues": [
            {
                "file": "src/a.ts",
                "exports": [{"name": "a"}, {"name": "b"}],
                "files": [],
                "duplicates": [[{"name": "c"}]],
            },
            {"file": "src/b.ts", "types": [{"name": "T"}], "dependencies": []},
        ]
    }
)

EXPECTED = {
    "allowed_comments_external": 1,
    "allowed_comments_issue": 1,
    "allowed_comments_license": 1,
    "banned_api_exceptions": 2,
    "ci_quarantine": 1,
    "disallowed_comments": 2,
    "eslint_exceptions": 3,
    "import_linter_exceptions": 2,
    "knip_unused": 4,
    "prose_only_rules": 2,
    "suppressions": 1,
    "ts_any": 2,
    "ty_baseline_entries": 2,
}


KEYS = parse(METRICS_TOML, "trust.toml").metric_keys()


def root_config(root: Path) -> Config:
    return load(root / "trust.toml", "trust.toml", "trust.toml")


def collect(root: Path) -> tuple[dict[str, int | None], list[checks.Problem]]:
    return metrics.collect(root, root_config(root), fake_knip)


def fake_knip(root: Path, directory: str) -> str:
    return KNIP_OUTPUT


def fixture_contract() -> settings.EslintContract:
    return settings.parse_contract((ESLINT_FIXTURE / "eslint-contract.json").read_text(encoding="utf-8"))


def fake_eslint(root: Path, directory: str, files: list[str], rules: list[str], probes: list[dict[str, str]]) -> dict:
    exceptions = json.loads((root / directory / "eslint-exceptions.json").read_text(encoding="utf-8"))
    contract = settings.parse_contract((root / directory / "eslint-contract.json").read_text(encoding="utf-8"))
    results = {}
    for file in files:
        found = {}
        for rule in rules:
            exempt = file in exceptions.get(rule, []) or contract.exempt_file(rule, file)
            found[rule] = [0] if exempt else [2]
        results[file] = {"ignored": False, "rules": found}
    messages = [{"ruleId": item.rule, "line": item.line} for item in contract.expect for _ in range(item.count)]
    return {
        "files": results,
        "probes": [{"filePath": probe["filePath"], "ignored": False, "messages": list(messages)} for probe in probes],
    }


EslintChange = Callable[[dict], None]


def eslint_with(change: EslintChange) -> metrics.EslintRunner:
    def runner(root: Path, directory: str, files: list[str], rules: list[str], probes: list[dict[str, str]]) -> dict:
        results = fake_eslint(root, directory, files, rules, probes)
        change(results)
        return results

    return runner


def write_sources(root: Path) -> None:
    write(root, "trust.toml", METRICS_TOML)
    write(root, PYPROJECT_PATH, PYPROJECT)
    write(root, "docs/rules.md", STANDARDS)
    write(root, TY_BASELINE_PATH, json.dumps(TY_BASELINE, indent=2) + "\n")
    write(
        root,
        "web/eslint-exceptions.json",
        json.dumps({"r1": ["src/a.ts"], "r2": [], "r3": ["src/b.ts", "src/c.ts"]}),
    )
    write(root, "web/eslint-contract.json", (ESLINT_FIXTURE / "eslint-contract.json").read_text(encoding="utf-8"))
    write(root, "web/src/a.ts", "export const a: any = 1 as any; // 설명\n")
    write(root, "web/src/index.tsx", "export const index = 1;\n")
    for path in ("core/database.py", "src/x.py", "src/y.py"):
        write(root, f"app/{path}", "value = 1\n")
    write(root, "app/tests/test_q.py", "import pytest\n\n\n@pytest.mark.ci_quarantine\ndef test_a():\n    pass\n")
    write(
        root,
        "app/src/m.py",
        '"""모듈 설명"""\n\n'
        "x = 1  # 외부: 바깥 제약\n"
        "y = 2  # noqa: F401 이유 · 만료 2027-01-01\n"
        "# 라이선스: MIT\n"
        "# https://github.com/o/r/issues/1\n",
    )


def run(
    root: Path,
    *argv: str,
    env: dict[str, str] | None = None,
    knip: metrics.KnipRunner = fake_knip,
    eslint: metrics.EslintRunner = fake_eslint,
) -> int:
    return cli.run(["--root", str(root), *argv], env=TODAY_ENV if env is None else env, knip=knip, eslint=eslint)


def run_metrics(
    root: Path, *argv: str, env: dict[str, str] | None = None, eslint: metrics.EslintRunner = fake_eslint
) -> int:
    return run(root, "metrics", *argv, env=env, eslint=eslint)


def baseline_with(**changes: int) -> str:
    return metrics.render_baseline({**EXPECTED, **changes})


def check_fails_without_label(
    root: Path, capsys, needle: str, eslint: metrics.EslintRunner = fake_eslint, options: Sequence[str] = ()
) -> str:
    assert run_metrics(root, *options, "--check", "--base", "HEAD", eslint=eslint) == 1
    out = capsys.readouterr().out
    assert needle in out, out
    assert "baseline-approved" in out
    assert run_metrics(root, *options, "--check", "--base", "HEAD", env=APPROVED_ENV, eslint=eslint) == 0
    capsys.readouterr()
    return out


def tool_version(path: str) -> str | None:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    parts = result.stdout.split()
    return parts[1] if result.returncode == 0 and len(parts) > 1 else None


def find_tool(name: str, version: str | None, candidates: Sequence[str | None] | None = None) -> str | None:
    if candidates is None:
        local = REPO / ".venv/bin" / name
        candidates = [str(local) if local.is_file() else None, shutil.which(name)]
    for candidate in candidates:
        if candidate and (found := tool_version(candidate)) is not None and version in (None, found):
            return candidate
    return None


def require_tool(name: str, version: str | None = None) -> str:
    found = find_tool(name, version)
    if found is None:
        pytest.skip(f"{name} {version or ''} 실행 파일이 없어 실제 도구 대조를 건너뛴다(정적 판정 시험은 따로 돈다)")
    return found


def real_frontend(tmp_path: Path) -> Path:
    for name in ("eslint.config.mjs", "eslint-exceptions.json", "eslint-contract.json"):
        write(tmp_path, f"web/{name}", (ESLINT_FIXTURE / name).read_text(encoding="utf-8"))
    (tmp_path / "web/node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
    write(tmp_path, "web/src/index.tsx", "export const index = 1;\n")
    return tmp_path


def contract_problems(root: Path) -> list[str]:
    files = ["src/index.tsx"]
    contract = settings.parse_contract((root / "web/eslint-contract.json").read_text(encoding="utf-8"))
    exceptions = json.loads((root / "web/eslint-exceptions.json").read_text(encoding="utf-8"))
    rules = contract.rules(exceptions)
    results = metrics.run_eslint(root, "web", files, rules, settings.eslint_probes(files, contract))
    found = settings.eslint_contract_problems("web/eslint.config.mjs", results["probes"], exceptions, contract)
    return [problem.render() for problem in found]
