import json
import subprocess

import pytest
from support import APPROVED_ENV, BASELINE_PATH, commit_all, git, init_repo, require_tool, tool_version, write

from trust_checks import cli, config, metrics

PRINTS = """[[metrics.ruff_rules]]
key = "print_calls"
pyproject = "app/pyproject.toml"
codes = ["T201"]
roots = ["app/src", "app/main.py"]
exclude = ["app/src/generated/"]
version = "0.15.0"
fix = "print 대신 모듈의 로거를 쓴다."
"""
IMPORTS = """[[metrics.ruff_rules]]
key = "unused_imports"
pyproject = "app/pyproject.toml"
codes = ["F401"]
roots = ["app/main.py"]
version = "0.15.1"
fix = "쓰지 않는 import 를 지운다."
"""
PYPROJECT = """[tool.ruff]
target-version = "py313"

[tool.ruff.lint]
ignore = ["F401"]

[tool.ruff.lint.per-file-ignores]
"src/target.py" = ["T201"]
"""
SOURCE = "app/src/target.py"


@pytest.fixture
def ruff_repo(tmp_path):
    root = init_repo(tmp_path / "repo")
    write(root, "trust.toml", '[comments]\nexclude = ["app/**"]\n\n' + PRINTS + "\n" + IMPORTS)
    write(root, "app/pyproject.toml", PYPROJECT)
    write(root, SOURCE, "value = 1\nprint(value)\n")
    write(root, "app/src/generated/x.py", "print(0)\n")
    write(root, "app/main.py", "import os\n")
    write(root, "app/outside.py", "print(0)\n")
    return root


def run_metrics(root, *args, ruff, env=None):
    return cli.run(["--root", str(root), "metrics", *args], ruff=ruff, env={} if env is None else env)


def fake_ruff(root, command):
    code = command[command.index("--select") + 1]
    prefix = {"T201": "print(", "F401": "import "}[code]
    diagnostics = [
        {"filename": str(root / path), "location": {"row": row}, "code": code}
        for path in command[command.index("--") + 1 :]
        for row, line in enumerate((root / path).read_text(encoding="utf-8").splitlines(), start=1)
        if line.startswith(prefix)
    ]
    return subprocess.CompletedProcess(command, int(bool(diagnostics)), json.dumps(diagnostics), "")


def test_pinned_commands_use_isolation_and_explicit_paths(ruff_repo):
    commands = []

    def subprocess_result(root, command):
        assert root == ruff_repo.resolve()
        commands.append(command)
        return fake_ruff(root, command)

    assert run_metrics(ruff_repo, ruff=subprocess_result) == 0
    flags = ["check", "--isolated", "--ignore-noqa", "--no-cache", "--output-format", "json", "--select"]
    target = ["--target-version", "py313", "--"]
    assert commands == [
        ["uvx", "ruff@0.15.0", *flags, "T201", *target, "app/main.py", SOURCE],
        ["uvx", "ruff@0.15.1", *flags, "F401", *target, "app/main.py"],
    ]


def test_metrics_update_check_increase_and_decrease(ruff_repo, capsys):
    assert run_metrics(ruff_repo, "--update", ruff=fake_ruff) == 0
    baseline = json.loads((ruff_repo / BASELINE_PATH).read_text(encoding="utf-8"))
    assert baseline["print_calls"] == baseline["unused_imports"] == 1
    commit_all(ruff_repo, "baseline")
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=fake_ruff) == 0
    capsys.readouterr()

    write(ruff_repo, SOURCE, "value = 1\nprint(value)\nprint(2)\n")
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=fake_ruff) == 1
    out = capsys.readouterr().out
    assert "print_calls 가 기준선 1 보다 늘었다(현재 2)" in out
    assert "print 대신 모듈의 로거를 쓴다." in out
    assert f"{SOURCE}:3 (T201)" in out
    assert f"{SOURCE}:2 (T201)" not in out
    assert "unused_imports=1 (기준선 1)" in out
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=fake_ruff, env=APPROVED_ENV) == 0

    write(ruff_repo, SOURCE, "value = 1\n")
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=fake_ruff) == 0
    assert "알림: print_calls 가 기준선 1 보다 줄었다(현재 0)" in capsys.readouterr().out


def test_real_ruff_counts_ignored_and_noqa_violations(ruff_repo, capsys):
    binary = require_tool("ruff")
    version = tool_version(binary)
    trust = ruff_repo / "trust.toml"
    write(ruff_repo, "trust.toml", trust.read_text().replace("0.15.0", str(version)).replace("0.15.1", str(version)))

    def local_ruff(root, command):
        assert command[:2] == ["uvx", f"ruff@{version}"]
        return metrics.run_ruff(root, [binary, *command[2:]])

    write(ruff_repo, SOURCE, "# ruff: noqa: T201\nprint(1)  # noqa: T201\nprint(2)\n")
    assert run_metrics(ruff_repo, "--update", ruff=local_ruff) == 0
    baseline = json.loads((ruff_repo / BASELINE_PATH).read_text(encoding="utf-8"))
    assert (baseline["print_calls"], baseline["unused_imports"]) == (2, 1)
    commit_all(ruff_repo, "baseline")
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=local_ruff) == 0
    write(ruff_repo, SOURCE, "# ruff: noqa: T201\nprint(1)  # noqa: T201\nprint(2)\nprint(3)  # noqa: T201\n")
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=local_ruff) == 1
    assert f"{SOURCE}:4 (T201)" in capsys.readouterr().out


def test_missing_source_and_ruff_failure_cannot_update_baseline(ruff_repo, capsys):
    (ruff_repo / "app/main.py").unlink()
    assert run_metrics(ruff_repo, "--update", ruff=fake_ruff) == 1
    assert "roots 에 적은 파일 또는 폴더가 없다" in capsys.readouterr().out
    assert not (ruff_repo / BASELINE_PATH).exists()

    def broken(root, command):
        return subprocess.CompletedProcess(command, 2, "[]", "unknown rule")

    write(ruff_repo, "app/main.py", "import os\n")
    assert run_metrics(ruff_repo, "--update", ruff=broken) == 1
    assert "ruff 실행 실패(종료 2): unknown rule" in capsys.readouterr().out
    assert not (ruff_repo / BASELINE_PATH).exists()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (PRINTS.replace('key = "print_calls"\n', ""), "key 이 없다"),
        (PRINTS.replace('codes = ["T201"]', "codes = []"), "codes 이 비었다"),
        (PRINTS.replace("0.15.0", "latest"), "정확한 Ruff 판"),
        (PRINTS + "\n" + IMPORTS.replace("unused_imports", "print_calls"), "'print_calls' 가"),
    ],
)
def test_ruff_config_rejects_invalid_entries(text, message):
    with pytest.raises(config.ConfigError, match=message):
        config.parse(text, "trust.toml")


def test_changed_ruff_scope_and_added_allow_names_require_label(ruff_repo, capsys):
    write(ruff_repo, "trust.toml", '[repo]\nforbid_names = [".env.*"]\n\n' + PRINTS)
    assert run_metrics(ruff_repo, "--update", ruff=fake_ruff) == 0
    commit_all(ruff_repo, "baseline")
    write(
        ruff_repo,
        "trust.toml",
        '[repo]\nforbid_names = [".env.*"]\nallow_names = [".env.example"]\n\n'
        + PRINTS.replace('"app/src/generated/"', '"app/src/target.py"'),
    )
    assert run_metrics(ruff_repo, "--check", "--base", "HEAD", ruff=fake_ruff) == 1
    out = capsys.readouterr().out
    assert '[[metrics.ruff_rules]] (key = "print_calls") 설정이 기준 ref 와 다르다' in out
    assert "[repo] allow_names 에 '.env.example' 를 더했다" in out


@pytest.mark.parametrize("path", ["keys/test.pem", "service/.env.local", "service/.env", "app/CLAUDE.md"])
def test_repo_glob_reports_only_tracked_path(tmp_path, capsys, path):
    root = init_repo(tmp_path / "repo")
    write(
        root,
        "trust.toml",
        '[repo]\nforbid_names = ["CLAUDE.md", "*.pem", ".env", ".env.*"]\nallow_names = [".env.example"]\n',
    )
    write(root, "nested/.env.example", "template value\n")
    git(root, "add", "nested/.env.example")
    write(root, path, "FAKE_SECRET_CONTENT\n")
    assert cli.run(["--root", str(root), "repo"]) == 0
    capsys.readouterr()
    git(root, "add", "--", path)
    assert cli.run(["--root", str(root), "repo"]) == 1
    output = capsys.readouterr().out
    assert output.startswith(f"{path}:1: ")
    assert "FAKE_SECRET_CONTENT" not in output
