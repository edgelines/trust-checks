import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from support import (
    APPROVED_ENV,
    NODE_MODULES,
    BASELINE_PATH,
    COMMENTS_TOML,
    EXPECTED,
    KEYS,
    METRICS_TOML,
    REPO,
    TODAY_ENV,
    baseline_with,
    check_fails_without_label,
    commit_all,
    fake_eslint,
    fake_knip,
    git,
    replace_in,
    require_tool,
    run,
    run_metrics,
    write,
)

from trust_checks import cli, config, metrics, settings
from trust_checks.config import ConfigError, parse


def config_error(text: str) -> str:
    with pytest.raises(ConfigError) as error:
        parse(text, "trust.toml")
    return str(error.value)


class TestConfigParsing:
    def test_empty_file_uses_defaults(self):
        found = parse("", "trust.toml")
        assert found.baseline == "trust-baseline.json"
        assert found.node_modules is None
        assert found.exclude == ()
        assert found.metric_keys() == (
            "suppressions",
            "allowed_comments_external",
            "allowed_comments_license",
            "allowed_comments_issue",
            "disallowed_comments",
        )

    def test_full_example_orders_keys(self):
        assert parse(METRICS_TOML, "trust.toml").metric_keys() == (
            "import_linter_exceptions",
            "banned_api_exceptions",
            "eslint_exceptions",
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

    def test_key_override(self):
        text = '[metrics.import_linter]\nkey = "services_to_routes_imports"\npyproject = "p.toml"\ncontract = "c"\n'
        assert parse(text, "trust.toml").metric_keys()[0] == "services_to_routes_imports"

    def test_defaults_inside_sections(self):
        text = '[metrics.ci_quarantine]\ntests = "tests/"\n\n[metrics.prose_only_rules]\ndoc = "d.md"\n'
        found = parse(text, "trust.toml")
        quarantine = found.get(config.Quarantine)
        assert quarantine is not None
        assert (quarantine.tests, quarantine.marker) == ("tests", quarantine.key)
        assert quarantine.key in KEYS
        assert found.get(config.ProseOnly) == config.ProseOnly("prose_only_rules", "d.md", "규칙과 강제 수단")

    def test_root_directory_forms(self):
        assert parse('[repo]\ntool_settings_dirs = [".", "./a/", "b"]\n', "t").tool_settings_dirs == ("", "a", "b")

    @pytest.mark.parametrize(
        ("text", "needle"),
        [
            ("[other]\nx = 1\n", "최상위 에 모르는 열쇠 'other'"),
            ('[trust]\nbase = "x"\n', "[trust] 에 모르는 열쇠 'base'"),
            ('[comments]\nexclude = []\ninclude = ["x"]\n', "[comments] 에 모르는 열쇠 'include'"),
            ('[repo]\nforbid = ["x"]\n', "[repo] 에 모르는 열쇠 'forbid'"),
            ('[metrics.mypy]\nx = "y"\n', "[metrics] 에 모르는 열쇠 'mypy'"),
            ('[metrics.ty]\npyproject = "a"\nbaseline = "b"\nlevel = "x"\n', "[metrics.ty] 에 모르는 열쇠 'level'"),
            ('[metrics.ty]\npyproject = "a"\n', "[metrics.ty] 에 baseline 이 없다"),
            ("[metrics.ts_any]\nroots = []\n", "[metrics.ts_any] 의 roots 이 비었다"),
            ('[metrics.ts_any]\nroots = "src"\n', "[metrics.ts_any] 의 roots 은 비지 않은 문자열 목록"),
            ("[trust]\nbaseline = 1\n", "[trust] 의 baseline 은 비지 않은 문자열"),
            ('[comments]\nexclude = [""]\n', "[comments] 의 exclude 은 비지 않은 문자열 목록"),
            ('trust = "x"\n', "trust 는 표여야 한다"),
            ('[metrics]\nty = "x"\n', "metrics.ty 는 표여야 한다"),
            ('[metrics.knip]\ndir = "web"\nkey = "Bad-Key"\n', "key 'Bad-Key' 는 쓸 수 없는 이름이다"),
            (
                '[metrics.knip]\ndir = "web"\nkey = "ts_any"\n\n[metrics.ts_any]\nroots = ["s"]\n',
                "[metrics.knip] 의 열쇠 이름 'ts_any' 가 [metrics.ts_any] 과 겹친다",
            ),
            ('[metrics.knip]\ndir = "web"\nkey = "suppressions"\n', "'suppressions' 가 주석 기본 지표 과 겹친다"),
            ("[trust\n", "TOML 을 읽지 못했다"),
        ],
    )
    def test_bad_config_names_fix(self, text, needle):
        message = config_error(text)
        assert message.startswith("trust.toml: ")
        assert needle in message
        assert " — " in message

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError) as error:
            config.load(tmp_path / "trust.toml", "trust.toml")
        assert "trust.toml 가 없다 — " in str(error.value)


class TestCommandLine:
    def test_missing_config_is_error(self, tmp_path, capsys):
        root = tmp_path / "bare"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        assert run(root, "repo") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: trust.toml 가 없다 — ")

    def test_unknown_key_is_error(self, repo, capsys):
        write(repo, "trust.toml", COMMENTS_TOML + '\n[metrics.ty]\npyproject = "a"\nbaseline = "b"\nextra = 1\n')
        assert run(repo, "expired") == 1
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("trust_checks: 오류: trust.toml: [metrics.ty] 에 모르는 열쇠 'extra'")

    def test_root_defaults_to_git_top_of_cwd(self, repo, capsys):
        sub = repo / "app"
        assert cli.run(["repo"], env=TODAY_ENV, cwd=sub) == 0
        assert capsys.readouterr().out.strip() == "repo: 문제 없음"

    def test_outside_git_without_root_is_error(self, tmp_path, capsys):
        assert cli.run(["repo"], env=TODAY_ENV, cwd=tmp_path) == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: 현재 폴더가 git 저장소가 아니다")

    def test_missing_root_is_error(self, tmp_path, capsys):
        assert cli.run(["--root", str(tmp_path / "none"), "repo"], env=TODAY_ENV) == 1
        assert "폴더가 없다" in capsys.readouterr().out

    def test_options_after_command(self, repo, capsys):
        assert cli.run(["repo", "--root", str(repo)], env=TODAY_ENV) == 0
        assert capsys.readouterr().out.strip() == "repo: 문제 없음"

    def test_config_option_is_relative_to_cwd(self, repo, tmp_path, capsys):
        write(
            tmp_path,
            "conf/other.toml",
            COMMENTS_TOML.replace('forbid_names = ["CLAUDE.md"]', 'forbid_names = ["NOTES.md"]'),
        )
        write(repo, "app/NOTES.md", "x\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 0
        args = ["--root", str(repo), "--config", "conf/other.toml", "repo"]
        assert cli.run(args, env=TODAY_ENV, cwd=tmp_path) == 1
        out = capsys.readouterr().out.splitlines()
        assert out[-2].startswith("app/NOTES.md:1: 추적 파일 NOTES.md 가 있다 — ")

    def test_module_entry_point(self, repo):
        result = subprocess.run(
            [sys.executable, "-m", "trust_checks", "--root", str(repo), "expired"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "TRUST_TODAY": "2026-11-16"},
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "expired: 만료가 지난 검사 끄기 표시 없음"

    def test_help_names_command(self, capsys):
        with pytest.raises(SystemExit):
            cli.run(["--help"])
        assert "trust-checks" in capsys.readouterr().out

    def test_ts_file_without_node_modules_is_error(self, repo, capsys):
        replace_in(repo, "trust.toml", f'node_modules = "{REPO / "node_modules"}"\n', "")
        write(repo, "web/src/a.ts", "export {};\n")
        assert run(repo, "comments", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: TS/JS 대상 파일이 있는데 trust.toml [trust] node_modules 가 없다")

    def test_no_ts_file_needs_no_node_modules(self, repo, capsys):
        replace_in(repo, "trust.toml", f'node_modules = "{REPO / "node_modules"}"\n', "")
        assert run(repo, "expired") == 0
        capsys.readouterr()


class TestRepoSettings:
    def test_root_tool_settings_dir(self, repo, capsys):
        replace_in(repo, "trust.toml", 'tool_settings_dirs = ["app"]', 'tool_settings_dirs = ["."]')
        write(repo, "ruff.toml", "line-length = 1\n")
        write(repo, "pyproject.toml", "[tool.ruff]\nline-length = 1\n")
        write(repo, "sub/pyproject.toml", "[tool.ty]\nx = 1\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out.splitlines()
        assert [line.split(": ")[0] for line in out[:-1]] == ["ruff.toml:1", "sub/pyproject.toml:1"]
        assert "pyproject.toml 한 곳" in out[0]

    def test_deepest_directory_owns_its_pyproject(self, repo, capsys):
        replace_in(repo, "trust.toml", 'tool_settings_dirs = ["app"]', 'tool_settings_dirs = [".", "app"]')
        write(repo, "app/pyproject.toml", "[tool.ruff]\nline-length = 1\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 0
        capsys.readouterr()

    def test_without_settings_only_always_on_rules(self, repo, capsys):
        write(repo, "trust.toml", "")
        write(repo, "app/CLAUDE.md", "x\n")
        write(repo, "app/ruff.toml", "x = 1\n")
        write(repo, ".rgignore", "x\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out.splitlines()
        assert [line.split(": ")[0] for line in out[:-1]] == [".rgignore:1"]


class TestGenericCode:
    def test_prefixes_follow_letters(self):
        assert settings.code_prefixes("E501") == {"ALL": 0, "E": 1, "E5": 2, "E50": 3, "E501": 4}
        assert list(settings.code_prefixes("TID251")) == ["ALL", "TID", "TID2", "TID25", "TID251"]

    def test_other_code_selection(self):
        data = {"tool": {"ruff": {"lint": {"select": ["E"], "ignore": ["E50"]}}}}
        assert not settings.code_enabled(data, "E501")
        assert settings.code_enabled(data, "E402")

    def test_message_names_configured_code(self, metrics_repo, capsys):
        replace_in(metrics_repo, "trust.toml", 'code = "TID251"', 'code = "F401"')
        replace_in(metrics_repo, "app/pyproject.toml", 'select = ["F", "TID251"]', 'select = ["TID251"]')
        commit_all(metrics_repo, "code")
        write(metrics_repo, BASELINE_PATH, baseline_with(banned_api_exceptions=1))
        commit_all(metrics_repo, "baseline")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert "ruff 가 F401 을 고르지 않는다" in capsys.readouterr().out


def write_toml(root: Path, text: str) -> None:
    write(root, "trust.toml", text)


class TestBaselineKeySet:
    def test_worktree_baseline_with_disabled_metric_fails(self, metrics_repo, capsys):
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline({**EXPECTED, "other_metric": 0}))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        assert "기준선의 열쇠 other_metric 는 켜진 지표가 아니다" in capsys.readouterr().out

    def test_turning_metric_off_needs_label(self, metrics_repo, capsys):
        start = METRICS_TOML.index("[metrics.knip]")
        end = METRICS_TOML.index("[metrics.ci_quarantine]")
        write_toml(metrics_repo, METRICS_TOML[:start] + METRICS_TOML[end:])
        values = {key: value for key, value in EXPECTED.items() if key != "knip_unused"}
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(values))
        out = check_fails_without_label(
            metrics_repo, capsys, "기준 ref 기준선에 있는 지표 knip_unused 가 지금 꺼져 있다"
        )
        assert "knip_unused=" not in out

    def test_renaming_key_needs_label(self, metrics_repo, capsys):
        write_toml(metrics_repo, METRICS_TOML.replace("[metrics.knip]\n", '[metrics.knip]\nkey = "knip_issues"\n'))
        values = {**{key: value for key, value in EXPECTED.items() if key != "knip_unused"}, "knip_issues": 9}
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(values))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert "지표 knip_unused 가 지금 꺼져 있다" in out
        assert (
            "알림: 기준 ref 기준선에 knip_issues 가 없다 — 새로 켠 지표로 보고 작업 트리 기준선 값 9 과 비교한다" in out
        )

    def test_new_metric_compares_with_worktree_value(self, metrics_repo, capsys):
        start = METRICS_TOML.index("[metrics.knip]")
        end = METRICS_TOML.index("[metrics.ci_quarantine]")
        write_toml(metrics_repo, METRICS_TOML[:start] + METRICS_TOML[end:])
        values = {key: value for key, value in EXPECTED.items() if key != "knip_unused"}
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(values))
        commit_all(metrics_repo, "knip off")
        write_toml(metrics_repo, METRICS_TOML)
        write(metrics_repo, BASELINE_PATH, baseline_with(knip_unused=3))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert "알림: 기준 ref 기준선에 knip_unused 가 없다" in out
        assert "knip_unused 가 기준선 3 보다 늘었다(현재 4)" in out
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_key_override_keeps_existing_baseline_name(self, metrics_repo, capsys):
        write_toml(
            metrics_repo,
            METRICS_TOML.replace(
                "[metrics.import_linter]\n", '[metrics.import_linter]\nkey = "services_to_routes_imports"\n'
            ),
        )
        values = {key: value for key, value in EXPECTED.items() if key != "import_linter_exceptions"}
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline({**values, "services_to_routes_imports": 2}))
        commit_all(metrics_repo, "rename")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        assert capsys.readouterr().out.splitlines()[0] == "services_to_routes_imports=2 (기준선 2)"

    def test_only_base_metrics_without_sections(self, tmp_path, capsys):
        root = tmp_path / "plain"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        write(root, "trust.toml", "")
        write(root, "a.py", "x = 1  # 설명\n")
        commit_all(root, "base")
        assert run(root, "metrics", "--update") == 0
        written = json.loads((root / "trust-baseline.json").read_text(encoding="utf-8"))
        assert sorted(written) == sorted(parse("", "t").metric_keys())
        assert written["disallowed_comments"] == 1
        capsys.readouterr()


class TestConfigLoosening:
    def test_adding_comment_exclusion_needs_label(self, metrics_repo, capsys):
        write_toml(metrics_repo, METRICS_TOML + '\n[comments]\nexclude = ["app/**"]\n')
        out = check_fails_without_label(metrics_repo, capsys, "주석 검사 제외 'app/**' 를 새로 더했다")
        assert "trust.toml:" in out

    def test_removing_comment_exclusion_passes(self, metrics_repo, capsys):
        write_toml(metrics_repo, METRICS_TOML + '\n[comments]\nexclude = ["vendor/**"]\n')
        commit_all(metrics_repo, "exclude")
        write_toml(metrics_repo, METRICS_TOML)
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_removing_repo_rules_needs_label(self, metrics_repo, capsys):
        repo_rules = '\n[repo]\nforbid_names = ["CLAUDE.md"]\ntool_settings_dirs = ["app"]\n'
        write_toml(metrics_repo, METRICS_TOML + repo_rules)
        commit_all(metrics_repo, "repo rules")
        write_toml(metrics_repo, METRICS_TOML + "\n[repo]\nforbid_names = []\n")
        out = check_fails_without_label(metrics_repo, capsys, "[repo] forbid_names 에서 'CLAUDE.md' 를 뺐다")
        assert "[repo] tool_settings_dirs 에서 'app' 를 뺐다" in out

    def test_changing_section_needs_label(self, metrics_repo, capsys):
        write_toml(metrics_repo, METRICS_TOML.replace('roots = ["web/src"]', 'roots = ["web/src/none"]'))
        check_fails_without_label(metrics_repo, capsys, "[metrics.ts_any] 설정이 기준 ref 와 다르다")

    def test_widening_allow_list_needs_label(self, metrics_repo, capsys):
        write_toml(
            metrics_repo,
            METRICS_TOML.replace(
                'allow = ["core/database.py", "tests/**"]', 'allow = ["core/database.py", "tests/**", "src/x.py"]'
            ),
        )
        check_fails_without_label(metrics_repo, capsys, "[metrics.banned_api] 설정이 기준 ref 와 다르다")

    def test_moving_baseline_needs_label(self, metrics_repo, capsys):
        write_toml(metrics_repo, METRICS_TOML.replace(f'baseline = "{BASELINE_PATH}"', 'baseline = "other.json"'))
        write(metrics_repo, "other.json", metrics.render_baseline(EXPECTED))
        out = check_fails_without_label(metrics_repo, capsys, "[trust] baseline 경로를 바꿨다")
        assert "ts_any=2 (기준선 2)" in out
        write(metrics_repo, "other.json", baseline_with(ts_any=9))
        write(metrics_repo, "web/src/b.ts", "let b: any;\n")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 0
        assert "기준선 ts_any 올림(2 → 9)" in capsys.readouterr().out

    def test_adding_section_passes(self, metrics_repo, capsys):
        start = METRICS_TOML.index("[metrics.knip]")
        end = METRICS_TOML.index("[metrics.ci_quarantine]")
        write_toml(metrics_repo, METRICS_TOML[:start] + METRICS_TOML[end:])
        values = {key: value for key, value in EXPECTED.items() if key != "knip_unused"}
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(values))
        commit_all(metrics_repo, "knip off")
        write_toml(metrics_repo, METRICS_TOML)
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_config_outside_root_only_notifies(self, metrics_repo, tmp_path, capsys):
        write(tmp_path, "outside/trust.toml", METRICS_TOML)
        args = [
            "--root",
            str(metrics_repo),
            "--config",
            str(tmp_path / "outside/trust.toml"),
            "metrics",
            "--check",
            "--base",
            "HEAD",
        ]
        assert cli.run(args, env=TODAY_ENV, knip=fake_knip, eslint=fake_eslint) == 0
        assert "저장소 밖에 있어 기준 ref 설정과 비교하지 않는다" in capsys.readouterr().out


class TestEslintContractFile:
    def test_missing_contract_fails_even_with_label(self, metrics_repo, capsys):
        (metrics_repo / "web/eslint-contract.json").unlink()
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        assert "web/eslint-contract.json:1: eslint 계약 파일이 없다" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("text", "needle"),
        [
            ("{", "JSON 을 읽지 못했다"),
            ("[]", "최상위가 객체가 아니다"),
            ('{"files": [], "extra": 1}', "모르는 열쇠 extra"),
            (
                '{"files": ["src/**"], "reference": "r", "probe": {"path": "p", "lines": ["x"]}, "expect": []}',
                "expect 는 비지 않은 목록",
            ),
        ],
    )
    def test_bad_contract_fails_even_with_label(self, metrics_repo, capsys, text, needle):
        write(metrics_repo, "web/eslint-contract.json", text)
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        out = capsys.readouterr().out
        assert "eslint 계약 파일 모양이 틀렸다" in out
        assert needle in out

    def test_changing_contract_needs_label(self, metrics_repo, capsys):
        data = json.loads((metrics_repo / "web/eslint-contract.json").read_text(encoding="utf-8"))
        data["expect"] = data["expect"][:1]
        write(metrics_repo, "web/eslint-contract.json", json.dumps(data))
        check_fails_without_label(metrics_repo, capsys, "eslint 계약 파일이 기준 ref 와 다르다")

    def test_reformatting_contract_passes(self, metrics_repo, capsys):
        data = json.loads((metrics_repo / "web/eslint-contract.json").read_text(encoding="utf-8"))
        write(metrics_repo, "web/eslint-contract.json", json.dumps(data))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_exempt_globs(self):
        contract = settings.parse_contract(
            (REPO / "tests/fixtures/eslint/eslint-contract.json").read_text(encoding="utf-8")
        )
        rule = "@typescript-eslint/no-restricted-imports"
        assert contract.exempt_file(rule, "src/utils/auth.ts")
        assert contract.exempt_file(rule, "src/__tests__/a.ts")
        assert contract.exempt_file(rule, "src/pages/x/__tests__/deep/a.tsx")
        assert contract.exempt_file(rule, "src/a.test.tsx")
        assert not contract.exempt_file(rule, "src/a.ts")
        assert not contract.exempt_file("no-restricted-syntax", "src/utils/auth.ts")
        assert contract.selects("src/a.ts")
        assert not contract.selects("src/a.css")
        assert not contract.selects("other/a.ts")


def test_keys_match_expected():
    assert sorted(KEYS) == sorted(EXPECTED)


PL_CASES = [
    ('[tool.ruff.lint]\nselect = ["PLC0415"]\nignore = ["PL"]\n', True),
    ('[tool.ruff.lint]\nselect = ["PL"]\nignore = ["PLC"]\n', False),
    ('[tool.ruff.lint]\nselect = ["PLC"]\nignore = ["PL"]\n', True),
    ('[tool.ruff.lint]\nselect = ["PL"]\n', True),
    ('[tool.ruff.lint]\nselect = ["ALL"]\nignore = ["PL"]\n', False),
]


class TestReviewFixes:
    def test_changing_node_modules_needs_label(self, metrics_repo, tmp_path, capsys):
        other = tmp_path / "other_node_modules"
        other.symlink_to(NODE_MODULES, target_is_directory=True)
        replace_in(metrics_repo, "trust.toml", f'node_modules = "{NODE_MODULES}"', f'node_modules = "{other}"')
        check_fails_without_label(metrics_repo, capsys, "[trust] node_modules 경로를 바꿨다")

    def test_tracked_files_under_node_modules_fail(self, repo, capsys):
        replace_in(repo, "trust.toml", f'node_modules = "{NODE_MODULES}"', 'node_modules = "tools/nm"')
        write(repo, "tools/nm/typescript/lib/typescript.js", "module.exports = {};\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out
        assert out.startswith(
            "tools/nm/typescript/lib/typescript.js:1: [trust] node_modules(tools/nm) 아래 파일을 추적한다"
        )

    def test_root_must_be_repository_top(self, repo, capsys):
        assert cli.run(["--root", str(repo / "app"), "repo"], env=TODAY_ENV) == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: --root ")
        assert "저장소 뿌리가 아니다" in out

    def test_root_outside_git_fails(self, tmp_path, capsys):
        assert cli.run(["--root", str(tmp_path), "repo"], env=TODAY_ENV) == 1
        assert "git 저장소가 아니다" in capsys.readouterr().out

    @pytest.mark.parametrize(("text", "expected"), PL_CASES)
    def test_parent_linter_prefix_selection(self, text, expected):
        assert settings.code_enabled(tomllib.loads(text), "PLC0415") is expected

    @pytest.mark.parametrize(("text", "expected"), PL_CASES)
    def test_parent_linter_prefix_matches_real_ruff(self, tmp_path, text, expected):
        ruff = require_tool("ruff")
        write(tmp_path, "pyproject.toml", text)
        write(tmp_path, "a.py", "x = 1\n")
        result = subprocess.run(
            [ruff, "check", "--no-cache", "--show-settings", str(tmp_path / "a.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        enabled = result.stdout.split("linter.rules.enabled = [", 1)[1].split("]", 1)[0]
        assert ("(PLC0415)" in enabled) is expected

    def test_parent_linter_prefix_counts_exceptions(self):
        ignores = {"tool": {"ruff": {"lint": {"per-file-ignores": {"x.py": ["PL"], "y.py": ["P"]}}}}}
        assert list(settings.code_exceptions(ignores, "PLC0415")) == ["x.py"]
        assert "T" not in settings.code_prefixes("TID251")

    @pytest.mark.parametrize(
        ("text", "needle"),
        [
            ('[trust]\nbaseline = "/abs/b.json"\n', "[trust] 의 baseline '/abs/b.json' 는 절대 경로다"),
            (
                '[metrics.ty]\npyproject = "/etc/p.toml"\nbaseline = "b"\n',
                "[metrics.ty] 의 pyproject '/etc/p.toml' 는 절대 경로다",
            ),
            ('[metrics.ts_any]\nroots = ["../x"]\n', "[metrics.ts_any] 의 roots '../x' 에 '..' 가 있다"),
            ('[metrics.knip]\ndir = "a/../b"\n', "[metrics.knip] 의 dir 'a/../b' 에 '..' 가 있다"),
            ('[repo]\ntool_settings_dirs = ["/x"]\n', "[repo] 의 tool_settings_dirs '/x' 는 절대 경로다"),
            ('[trust]\nnode_modules = "../nm"\n', "[trust] 의 node_modules '../nm' 에 '..' 가 있다"),
        ],
    )
    def test_paths_stay_inside_repository(self, text, needle):
        message = config_error(text)
        assert needle in message
        assert " — " in message

    def test_absolute_node_modules_is_allowed(self):
        assert parse('[trust]\nnode_modules = "/opt/nm/"\n', "t").node_modules == "/opt/nm"

    def test_removing_section_without_base_baseline_needs_label(self, fresh_metrics_repo, capsys):
        start = METRICS_TOML.index("[metrics.knip]")
        end = METRICS_TOML.index("[metrics.ci_quarantine]")
        write_toml(fresh_metrics_repo, METRICS_TOML[:start] + METRICS_TOML[end:])
        values = {key: value for key, value in EXPECTED.items() if key != "knip_unused"}
        write(fresh_metrics_repo, BASELINE_PATH, metrics.render_baseline(values))
        check_fails_without_label(fresh_metrics_repo, capsys, "[metrics.knip] 절을 지워 지표를 껐다")
