import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from support import (
    API_RULES,
    APPROVED_ENV,
    BASELINE_PATH,
    COMMENTS_TOML,
    DB_ALLOW,
    EXPECTED,
    GATES,
    KEYS,
    KNIP_OUTPUT,
    MOVED_BODY,
    MOVED_LINES,
    PYPROJECT,
    PYPROJECT_PATH,
    REPO,
    STANDARDS,
    TODAY_ENV,
    TY_BASELINE,
    TY_BASELINE_PATH,
    baseline_with,
    categories,
    check_fails_without_label,
    collect,
    commit_all,
    contract_problems,
    eslint_with,
    find_tool,
    fixture_contract,
    git,
    init_repo,
    lines_of,
    problems,
    real_frontend,
    replace_in,
    require_tool,
    run,
    run_metrics,
    scan_text,
    write,
)

from trust_checks import checks, metrics, scan, settings
from trust_checks.scan import (
    DISALLOWED,
    EXTERNAL,
    ISSUE,
    LICENSE,
    SUPPRESSION,
    SUPPRESSION_REASON,
    is_excluded,
    is_target,
)

EXCLUDE = (
    f"{GATES}/rules/**",
    f"{GATES}/a_eval_*.py",
    f"{GATES}/a_base.py",
    f"{GATES}/e_breakdown.py",
    f"{GATES}/k_series.py",
    "logic/todo*.py",
    "app/TEST/**",
    "app/ipynb/**",
    "app/docker/**",
    "app/tests/fixtures/safe-rules/**",
    ".claude/skills/**",
    ".claude/agents/**",
    "vendor/payload/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/target/**",
    "**/dist/**",
    "**/*.lock",
    "**/*.lockb",
    "**/*-lock.json",
    "**/*-lock.yaml",
)


def count_services_to_routes(text: str) -> int:
    return metrics.count_import_linter(text, PYPROJECT_PATH, "services")


def count_direct_db_clients(text: str) -> int:
    return metrics.count_banned_api(text, PYPROJECT_PATH, "TID251", DB_ALLOW)


def count_prose_only_rules(text: str) -> int:
    return metrics.count_prose_only_rules(text, "docs/rules.md", "규칙과 강제 수단")


class TestPython:
    def test_hash_inside_strings_is_not_comment(self):
        text = 'a = "# no"\nb = f"{a} # no"\nc = """\n# no\n"""\nd = 1  # yes\n'
        assert lines_of("x.py", text) == [6]

    def test_docstrings_are_first_statement_of_module_class_function(self):
        text = (
            '"""module"""\n'
            "class A:\n"
            '    """class"""\n'
            "    def f(self):\n"
            '        """func"""\n'
            '        "not doc"\n'
            "async def g():\n"
            '    """async"""\n'
            "if True:\n"
            '    "not doc"\n'
            'x = "not doc"\n'
        )
        assert lines_of("x.py", text) == [1, 3, 5, 8]

    def test_multiline_docstring_is_one_comment(self):
        text = 'def f():\n    """첫 줄\n\n    둘째 줄\n    """\n    return 1\n'
        comments = scan_text("x.py", text).comments
        assert [(comment.line, comment.end_line) for comment in comments] == [(2, 5)]

    def test_shebang_and_encoding_lines_are_not_comments(self):
        text = "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\nx = 1\n"
        assert lines_of("x.py", text) == []

    def test_shebang_after_first_line_is_comment(self):
        assert lines_of("x.py", "x = 1\n#!/usr/bin/env python\n") == [2]

    def test_encoding_after_second_line_is_comment(self):
        assert lines_of("x.py", "x = 1\ny = 2\n# coding: utf-8\n") == [3]

    def test_runtime_doc_usage_is_still_comment(self):
        text = '"""설명"""\nimport argparse\nargparse.ArgumentParser(description=__doc__)\n'
        assert categories("x.py", text) == [DISALLOWED]

    def test_syntax_error_is_reported_as_file_problem(self):
        result = problems("x.py", "def f(:\n")
        assert len(result) == 1
        assert result[0].startswith("x.py:1: 파일을 읽지 못했다")


class TestTypeScript:
    def test_slashes_in_strings_templates_regex_are_not_comments(self):
        text = (
            "const a = 'http://x';\n"
            'const b = "/* no */";\n'
            "const c = `// no ${a} // no`;\n"
            "const d = /\\/\\/no/g;\n"
            "const e = 1; // yes\n"
            "/* yes */\n"
        )
        assert lines_of("x.ts", text) == [5, 6]

    def test_jsx_comment_is_comment_and_jsx_text_is_not(self):
        text = "export const A = () => (\n  <div>\n    http://x // no\n    {/* yes */}\n  </div>\n);\n"
        assert lines_of("x.tsx", text) == [4]

    def test_comment_inside_template_expression_is_comment(self):
        text = "const a = `x ${\n  1 // yes\n} y`;\n"
        assert lines_of("x.js", text) == [2]

    def test_shebang_and_triple_slash_directive_are_not_comments(self):
        text = '#!/usr/bin/env bun\n/// <reference types="vite/client" />\nexport {};\n'
        assert lines_of("x.mjs", text) == []

    def test_jsdoc_block_is_one_comment(self):
        text = "/**\n * 설명\n * 더\n */\nexport function f() {}\n"
        comments = scan_text("x.ts", text).comments
        assert [(comment.line, comment.end_line) for comment in comments] == [(1, 4)]

    def test_block_comment_body_classified_after_stars(self):
        text = "/**\n * 외부: 증권사 API 는 초당 20회\n */\nexport {};\n"
        assert categories("x.ts", text) == [EXTERNAL]


class TestRust:
    def test_strings_raw_strings_chars_are_not_comments(self):
        text = (
            'let a = "// no";\n'
            'let b = r#"/* no "quoted" */"#;\n'
            "let c = '/';\n"
            "fn f<'a>(x: &'a str) {} // yes\n"
            'let d = b"//no";\n'
        )
        assert lines_of("x.rs", text) == [4]

    def test_doc_comments_and_nested_blocks(self):
        text = "//! crate\n/// item\n/* a /* nested */ b */\nfn main() {}\n"
        comments = scan_text("x.rs", text).comments
        assert [comment.line for comment in comments] == [1, 2, 3]
        assert comments[2].body == "a /* nested */ b"

    def test_attributes_are_not_comments(self):
        assert lines_of("x.rs", "#![allow(dead_code)]\n#[derive(Debug)]\nstruct A;\n") == []


class TestShell:
    def test_parameter_expansions_and_quotes_are_not_comments(self):
        text = '#!/usr/bin/env bash\necho "$#" ${#name} "#x" \'#y\' a#b\necho $# # yes\nls;# yes\n'
        assert lines_of("x.sh", text) == [3, 4]

    def test_heredoc_body_is_not_comment(self):
        text = "cat <<EOF\n# no\nEOF\ncat <<-'END'\n\t# no\n\tEND\n# yes\n"
        assert lines_of("x.bash", text) == [7]

    def test_here_string_is_not_heredoc(self):
        assert lines_of("x.sh", "cat <<< word\n# yes\n") == [2]


class TestYaml:
    def test_block_scalar_text_is_string(self):
        text = (
            "jobs:\n"
            "  a:\n"
            "    steps:\n"
            "      - run: |  # yes\n"
            "          # no\n"
            "          echo hi # no\n"
            "\n"
            "          # no\n"
            "      - name: x # yes\n"
            "# yes\n"
        )
        assert lines_of("x.yml", text) == [4, 9, 10]

    def test_quoted_and_plain_scalars(self):
        text = "a: 'x # no'\nb: \"y # no\"\nc: it's # yes\nd: a#b\ne: ['p # no', q] # yes\nf: >-\n  # no\ng: 1\n"
        assert lines_of("x.yaml", text) == [3, 5]

    def test_multiline_quoted_scalar(self):
        text = "a: 'first\n  # no\n  last'\nb: 1 # yes\n"
        assert lines_of("x.yml", text) == [4]


class TestTomlAndDockerfile:
    def test_toml_strings_are_not_comments(self):
        text = "a = \"#no\"\nb = '#no'\nc = \"\"\"\n# no\n\"\"\"\nd = '''\n# no'''\ne = 1 # yes\n# yes\n"
        assert lines_of("x.toml", text) == [8, 9]

    def test_dockerfile_parser_directive_is_not_comment(self):
        text = "# syntax=docker/dockerfile:1\nFROM python:3.14\n# yes\nRUN echo '#no'\n"
        assert lines_of("Dockerfile.build", text) == [3]

    def test_dockerfile_directive_after_instruction_is_comment(self):
        assert lines_of("Dockerfile", "FROM a\n# syntax=x\n") == [2]


class TestAllowedComments:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("# 외부: KIS API 는 초당 20회만 받는다\n", EXTERNAL),
            ("# 외부:붙여씀\n", DISALLOWED),
            ("# 외부: \n", DISALLOWED),
            ("# 라이선스: MIT\n", LICENSE),
            ("# https://github.com/o/r/issues/2490 참고\n", ISSUE),
            ("# https://github.com/o/r/pull/2490\n", DISALLOWED),
            ("# http://github.com/edgelines/aox3/issues/12\n", DISALLOWED),
            ("# https://github.com/o/r/issues/abc\n", DISALLOWED),
            ("# 자동 생성 파일이다\n", DISALLOWED),
            ("#\n", DISALLOWED),
            ("# noqa: E501 긴 표 · 만료 2027-01-01\n", SUPPRESSION),
        ],
    )
    def test_prefixes(self, text, expected):
        assert categories("x.py", text) == [expected]

    def test_docstring_uses_same_prefix_rule(self):
        text = '"""외부: MongoDB 는 이 연산을 원자적으로 하지 않는다"""\n@app.get("/x")\ndef route():\n    """화면 설명"""\n'
        assert categories("x.py", text) == [EXTERNAL, DISALLOWED]

    def test_allowed_prefix_in_every_language(self):
        assert categories("x.ts", "// 외부: 브라우저 제약\n") == [EXTERNAL]
        assert categories("x.rs", "/// 외부: PyO3 제약\nfn a() {}\n") == [EXTERNAL]
        assert categories("x.sh", "# 외부: macOS cron 제약\n") == [EXTERNAL]
        assert categories("x.yml", "a: 1 # 외부: GitHub 제약\n") == [EXTERNAL]
        assert categories("x.toml", "# 외부: GHSA-xxxx 하한\n") == [EXTERNAL]

    def test_header_marker_does_not_exclude_file(self):
        text = "# 자동 생성 파일 — 손대지 마시오\n# 이 파일은 검사에서 뺀다\nx = 1\n"
        assert categories("x.py", text) == [DISALLOWED, DISALLOWED]


class TestSuppressionMarkers:
    @pytest.mark.parametrize(
        ("path", "text", "marker"),
        [
            ("x.py", "import os  # noqa: F401 이유 · 만료 2027-01-01\n", "noqa"),
            ("x.py", "# ruff: noqa: E501 이유 · 만료 2027-01-01\n", "noqa"),
            ("x.py", "a = 1  # type: ignore[assignment] 이유 · 만료 2027-01-01\n", "type: ignore"),
            ("x.py", "a = 1  # ty: ignore[invalid-assignment] 이유 · 만료 2027-01-01\n", "ty: ignore"),
            ("x.py", "a = 1  # pyright: ignore[reportX] 이유 · 만료 2027-01-01\n", "pyright: ignore"),
            ("x.py", "if a:  # pragma: no cover 이유 · 만료 2027-01-01\n    pass\n", "pragma: no cover"),
            ("x.py", "# fmt: off 이유 · 만료 2027-01-01\n", "fmt"),
            ("x.py", "# fmt: on 이유 · 만료 2027-01-01\n", "fmt"),
            ("x.ts", "// eslint-disable-next-line no-console -- 이유 · 만료 2027-01-01\n", "eslint-disable"),
            ("x.ts", "/* eslint-disable -- 이유 · 만료 2027-01-01 */\n", "eslint-disable"),
            ("x.ts", "a(); // eslint-disable-line -- 이유 · 만료 2027-01-01\n", "eslint-disable"),
            ("x.ts", "// @ts-ignore 이유 · 만료 2027-01-01\n", "@ts-ignore"),
            ("x.ts", "// @ts-expect-error 이유 · 만료 2027-01-01\n", "@ts-expect-error"),
            ("x.ts", "// @ts-nocheck 이유 · 만료 2027-01-01\n", "@ts-nocheck"),
            (
                "x.tsx",
                "const A = () => <div>{/* prettier-ignore 이유 · 만료 2027-01-01 */}</div>;\n",
                "prettier-ignore",
            ),
            ("x.js", "/* istanbul ignore next 이유 · 만료 2027-01-01 */\n", "istanbul ignore"),
            ("x.js", "/* c8 ignore next 이유 · 만료 2027-01-01 */\n", "c8 ignore"),
            ("x.sh", "# shellcheck disable=SC2086 이유 · 만료 2027-01-01\n", "shellcheck disable"),
        ],
    )
    def test_each_marker_is_recognized_with_reason_and_expiry(self, path, text, marker):
        result = scan_text(path, text)
        assert [comment.category for comment in result.comments] == [SUPPRESSION]
        assert [item.marker for item in result.suppressions] == [marker]
        assert result.suppressions[0].reason == "이유"
        assert result.suppressions[0].expiry == date(2027, 1, 1)
        assert problems(path, text) == []

    def test_marker_after_other_hash_comment_is_found(self):
        result = scan_text("x.py", "import os  # 외부: 설명 # noqa: F401 이유 · 만료 2027-01-01\n")
        assert [comment.category for comment in result.comments] == [EXTERNAL]
        assert [item.key for item in result.suppressions] == ["noqa: F401"]

    def test_reason_in_following_hash_segment(self):
        result = scan_text("x.py", "a = 1  # ty: ignore[x]  # 이유 · 만료 2027-01-01\n")
        assert result.suppressions[0].reason == "이유"
        assert result.suppressions[0].expiry == date(2027, 1, 1)

    def test_url_fragment_is_not_split(self):
        assert categories("x.py", "# https://github.com/o/r/issues/1#issuecomment-2\n") == [ISSUE]

    def test_missing_reason_or_expiry(self):
        for text in (
            "import os  # noqa: F401\n",
            "import os  # noqa: F401 이유만\n",
            "import os  # noqa: F401 · 만료 2027-01-01\n",
        ):
            result = problems("x.py", text)
            assert len(result) == 1
            assert result[0].startswith("x.py:1: 검사 끄기 표시 'noqa: F401' 에 이유·만료가 없다 — ")
            assert "2027-02-14" in result[0]

    def test_eslint_without_double_dash_has_no_reason(self):
        result = problems("x.ts", "a(); // eslint-disable-line no-console 이유 · 만료 2027-01-01\n")
        assert len(result) == 1
        assert "'-- <이유> · 만료 2027-02-14'" in result[0]

    def test_bad_date(self):
        result = problems("x.py", "import os  # noqa: F401 이유 · 만료 2027-02-30\n")
        assert len(result) == 1
        assert "만료 날짜가 틀렸다" in result[0]

    def test_reason_on_line_above(self):
        text = "# 검사 끄기: prettier 는 덧붙임을 못 받는다 · 만료 2027-01-01\n# fmt: off\nx = 1\n"
        result = scan_text("x.py", text)
        assert [comment.category for comment in result.comments] == [SUPPRESSION_REASON, SUPPRESSION]
        assert result.suppressions[0].form == "above"
        assert result.suppressions[0].expiry == date(2027, 1, 1)
        assert problems("x.py", text) == []

    def test_reason_above_for_inline_marker_on_code_line(self):
        text = "// 검사 끄기: 외부 타입이 틀렸다 · 만료 2027-01-01\nconst a = b as never; // @ts-ignore\n"
        assert problems("x.ts", text) == []

    def test_reason_above_without_marker_below_is_disallowed(self):
        text = "# 검사 끄기: 이유 · 만료 2027-01-01\nx = 1\n"
        result = problems("x.py", text)
        assert result == [f"x.py:1: {checks.orphan_message()}"]

    def test_reason_two_lines_above_does_not_count(self):
        text = "# 검사 끄기: 이유 · 만료 2027-01-01\n\nimport os  # noqa: F401\n"
        result = problems("x.py", text)
        assert len(result) == 2


class TestChangedFileRules:
    def test_rule1_existing_disallowed_comment_anywhere_fails(self):
        base = "x = 1  # 오래된 주석\ny = 2\n"
        current = "x = 1  # 오래된 주석\ny = 3\n"
        result = problems("app/a.py", current, base)
        assert result == [f"app/a.py:1: {checks.disallowed_message()}"]

    def test_rule1_message_format(self):
        result = problems("app/src/x.py", "x = 1\n\n\n\n\n\n\n\n\n\n\n# 설명\n")
        assert result == [
            "app/src/x.py:12: 허용 안 된 주석 — 지우고, 꼭 남길 내용이면 함수·변수 이름이나 시험으로 옮기거나 "
            "저장소 문서에 적는다. 바깥 제약이면 '외부: ', 이슈면 이슈 주소로 시작한다"
        ]

    def test_rule2_existing_unformatted_marker_fails(self):
        text = "import os  # noqa: F401\n"
        assert len(problems("a.py", text, base=text)) == 1

    def test_rule3_expired_marker_fails_even_with_label(self):
        text = "import os  # noqa: F401 이유 · 만료 2026-11-15\n"
        result = problems("a.py", text, base=text, is_approved=True)
        assert len(result) == 1
        assert "만료(2026-11-15)가 지났다" in result[0]

    def test_rule3_expiry_today_is_not_expired(self):
        text = "import os  # noqa: F401 이유 · 만료 2026-11-16\n"
        assert problems("a.py", text, base=text) == []

    def test_rule4_new_marker_must_expire_within_90_days(self):
        assert problems("a.py", "import os  # noqa: F401 이유 · 만료 2027-02-14\n") == []
        result = problems("a.py", "import os  # noqa: F401 이유 · 만료 2027-02-15\n")
        assert len(result) == 1
        assert "오늘부터 90일(2027-02-14)을 넘는다" in result[0]

    def test_rule4_unchanged_marker_keeps_far_expiry(self):
        text = "import os  # noqa: F401 이유 · 만료 2027-06-01\n"
        base = "import os  # noqa: F401 이유 · 만료 2027-06-01\n"
        assert problems("a.py", "x = 1\n" + text, base) == []

    def test_rule4_reason_change_counts_as_change(self):
        base = "import os  # noqa: F401 이유 · 만료 2027-06-01\n"
        current = "import os  # noqa: F401 새 이유 · 만료 2027-06-01\n"
        assert len(problems("a.py", current, base)) == 1

    def test_rule4_formatting_old_marker_is_a_change(self):
        base = "import os  # noqa: F401\n"
        assert len(problems("a.py", "import os  # noqa: F401 이유 · 만료 2027-03-01\n", base)) == 1
        assert problems("a.py", "import os  # noqa: F401 이유 · 만료 2027-02-01\n", base) == []

    def test_rule5_extension_needs_label(self):
        base = "import os  # noqa: F401 처음 이유 · 만료 2026-12-01\n"
        current = "import os  # noqa: F401 미루는 이유 · 만료 2027-01-15\n"
        result = problems("a.py", current, base)
        assert len(result) == 1
        assert "만료를 미뤘다(2026-12-01 → 2027-01-15)" in result[0]
        assert "baseline-approved" in result[0]
        assert problems("a.py", current, base, is_approved=True) == []

    def test_rule5_extension_needs_new_reason(self):
        base = "import os  # noqa: F401 이유 · 만료 2026-12-01\n"
        current = "import os  # noqa: F401 이유 · 만료 2027-01-15\n"
        result = problems("a.py", current, base, is_approved=True)
        assert len(result) == 1
        assert "이유를 새로 적는다" in result[0]

    def test_rule5_shortening_is_not_extension(self):
        base = "import os  # noqa: F401 이유 · 만료 2027-01-15\n"
        current = "import os  # noqa: F401 이유 · 만료 2026-12-01\n"
        assert problems("a.py", current, base) == []

    def test_pairing_uses_nearest_line_of_same_marker(self):
        base = "import os  # noqa: F401 가 · 만료 2026-12-01\n\n\n\n\nimport re  # noqa: F401 나 · 만료 2027-01-10\n"
        current = (
            "\n\nimport os  # noqa: F401 가 · 만료 2026-12-01\n\n\n\nimport re  # noqa: F401 나 · 만료 2027-01-10\n"
        )
        assert problems("a.py", current, base) == []
        swapped = (
            "\n\nimport os  # noqa: F401 가 · 만료 2027-01-10\n\n\n\nimport re  # noqa: F401 나 · 만료 2026-12-01\n"
        )
        result = problems("a.py", swapped, base)
        assert any("2026-12-01 → 2027-01-10" in line for line in result)

    def test_different_marker_text_is_new(self):
        base = "import os  # noqa: F401 이유 · 만료 2027-06-01\n"
        current = "import os  # noqa: F401, E501 이유 · 만료 2027-06-01\n"
        assert len(problems("a.py", current, base)) == 1


class TestExclusions:
    @pytest.mark.parametrize(
        "path",
        [
            f"{GATES}/rules/A1.md",
            f"{GATES}/rules/sub/x.py",
            f"{GATES}/a_eval_a1.py",
            f"{GATES}/a_base.py",
            f"{GATES}/e_breakdown.py",
            f"{GATES}/k_series.py",
            "logic/todo.py",
            "logic/todo_b.py",
            "app/TEST/x.py",
            "app/ipynb/x.py",
            "app/docker/x.sh",
            "app/tests/fixtures/safe-rules/x.py",
            ".claude/skills/a/b.py",
            ".claude/agents/a.ts",
            "vendor/payload/install.sh",
            "web/node_modules/x/index.js",
            "app/.venv/lib/x.py",
            "app/rust/a/target/debug/x.rs",
            "web/dist/main.js",
            "pnpm-lock.yaml",
            "app/uv.lock",
        ],
    )
    def test_excluded(self, path):
        assert is_excluded(path, EXCLUDE)
        assert not is_target(path, EXCLUDE)

    @pytest.mark.parametrize(
        "path",
        [
            f"{GATES}/helpers.py",
            f"{GATES}/a_breakdown.py",
            "logic/other.py",
            "app/tests/scripts/test_x.py",
            "web/src/App.tsx",
            ".github/workflows/ci-gate.yml",
            "app/pyproject.toml",
            "app/Dockerfile",
            "app/rust/engine/Dockerfile.wheel",
            ".claude/docs/x.sh",
        ],
    )
    def test_target(self, path):
        assert is_target(path, EXCLUDE)

    @pytest.mark.parametrize("path", ["README.md", "web/src/a.css", "app/x.json", "x.txt"])
    def test_other_kinds_are_not_target(self, path):
        assert not is_target(path, EXCLUDE)

    def test_new_rule_eval_file_is_excluded_automatically(self):
        assert is_excluded(f"{GATES}/a_eval_a999_p1.py", EXCLUDE)

    def test_nothing_is_excluded_without_list(self):
        assert is_target("web/node_modules/x/index.js")


class TestCommentsCommand:
    def test_base_includes_modified_added_untracked_and_renamed(self, repo, capsys):
        write(repo, "app/old.py", "x = 2  # 오래된 주석\n")
        write(repo, "app/new.py", "a = 1  # 새 주석\n")
        git(repo, "add", "app/new.py")
        write(repo, "app/untracked.py", "b = 1  # 추적 안 한 새 파일\n")
        git(repo, "mv", "app/move_me.py", "app/moved.py")
        write(repo, "app/moved.py", MOVED_BODY + "\nimport os  # noqa: F401 미룸 · 만료 2027-01-01\n")
        (repo / "app/gone.py").unlink()
        write(repo, f"{GATES}/a_eval_a1.py", "a = 2  # 제외 파일\n")
        write(repo, "docs/a.md", "# 다른 제목\n")
        assert run(repo, "comments", "--base", "HEAD") == 1
        out = capsys.readouterr().out.splitlines()
        reported = sorted({line.split(":")[0] for line in out[:-1]})
        assert reported == ["app/moved.py", "app/new.py", "app/old.py", "app/untracked.py"]
        assert any(
            line.startswith(f"app/moved.py:{MOVED_LINES + 2}: ") and "2026-12-01 → 2027-01-01" in line for line in out
        )
        assert out[-1] == f"comments: 바뀐 대상 파일 4개, 문제 {len(out) - 1}개"

    def test_base_uses_merge_base(self, repo, capsys):
        git(repo, "checkout", "-q", "-b", "feature")
        write(repo, "app/clean.py", "a = 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feature")
        git(repo, "checkout", "-q", "main")
        write(repo, "app/main_only.py", "b = 1  # main 에만 있는 주석\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main")
        git(repo, "checkout", "-q", "feature")
        assert run(repo, "comments", "--base", "main") == 0
        assert capsys.readouterr().out.strip() == "comments: 바뀐 대상 파일 1개, 문제 없음"

    def test_extension_passes_with_label_env(self, repo, capsys):
        write(repo, "app/move_me.py", MOVED_BODY + "import os  # noqa: F401 미루는 이유 · 만료 2027-01-01\n")
        assert run(repo, "comments", "--base", "HEAD") == 1
        assert run(repo, "comments", "--base", "HEAD", env=APPROVED_ENV) == 0
        capsys.readouterr()

    def test_staged_judges_index_content_only(self, repo, capsys):
        write(repo, "app/new.py", "a = 1\n")
        git(repo, "add", "app/new.py")
        write(repo, "app/new.py", "a = 1  # 스테이징 안 한 주석\n")
        write(repo, "app/keep.py", "y = 2  # 스테이징 안 한 수정\n")
        assert run(repo, "comments", "--staged") == 0
        assert capsys.readouterr().out.strip() == "comments: 바뀐 대상 파일 1개, 문제 없음"
        git(repo, "add", "app/keep.py")
        assert run(repo, "comments", "--staged") == 1
        assert capsys.readouterr().out.splitlines()[0].startswith("app/keep.py:1: 허용 안 된 주석")

    def test_staged_in_repo_without_commits(self, tmp_path, capsys):
        root = init_repo(tmp_path / "empty")
        write(root, "trust.toml", COMMENTS_TOML)
        write(root, "a.py", "a = 1  # 주석\n")
        git(root, "add", "a.py")
        assert run(root, "comments", "--staged") == 1
        assert capsys.readouterr().out.startswith("a.py:1: ")

    def test_bad_ref_fails(self, repo, capsys):
        assert run(repo, "comments", "--base", "no-such-ref") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: ")
        assert "git merge-base" in out

    def test_bad_today_env_fails(self, repo, capsys):
        assert run(repo, "comments", "--base", "HEAD", env={"TRUST_TODAY": "2026/11/16"}) == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: ")
        assert "TRUST_TODAY" in out

    def test_base_or_staged_is_required(self, repo):
        with pytest.raises(SystemExit):
            run(repo, "comments")


class TestExpiredCommand:
    def test_reports_expired_markers_in_tracked_files(self, repo, capsys):
        write(repo, "app/keep.py", "import os  # noqa: F401 이유 · 만료 2026-11-01\nimport re  # noqa: F401\n")
        write(repo, f"{GATES}/a_eval_a1.py", "import os  # noqa: F401 이유 · 만료 2026-01-01\n")
        git(repo, "add", "-A")
        assert run(repo, "expired") == 1
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 2
        assert out[0].startswith("app/keep.py:1: 검사 끄기 표시 'noqa: F401' 의 만료(2026-11-01)가 지났다 — ")
        assert out[1] == "expired: 만료가 지난 검사 끄기 표시 1개"

    def test_none_expired(self, repo, capsys):
        assert run(repo, "expired") == 0
        assert capsys.readouterr().out.strip() == "expired: 만료가 지난 검사 끄기 표시 없음"

    def test_today_override_changes_result(self, repo, capsys):
        assert run(repo, "expired", env={"TRUST_TODAY": "2026-12-02"}) == 1
        assert f"app/move_me.py:{MOVED_LINES + 1}: " in capsys.readouterr().out


class TestRepoCommand:
    def test_clean_repo_passes(self, repo, capsys):
        assert run(repo, "repo") == 0
        assert capsys.readouterr().out.strip() == "repo: 문제 없음"

    def test_tracked_claude_md_fails(self, repo, capsys):
        write(repo, "app/CLAUDE.md", "x\n")
        assert run(repo, "repo") == 0
        git(repo, "add", "app/CLAUDE.md")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out.splitlines()
        assert out[-2].startswith("app/CLAUDE.md:1: 추적 파일 CLAUDE.md 가 있다 — ")

    def test_tracked_disabled_file_fails(self, repo, capsys):
        write(repo, "web/src/__tests__/a.test.tsx.disabled", "x\n")
        write(repo, "old.disabled/b.txt", "x\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 3
        assert out[-1] == "repo: 문제 2개"


def test_today_defaults_to_korea_date():
    korea = (datetime.now(UTC) + timedelta(hours=9)).date()
    assert checks.today({}) == korea
    assert checks.today({"TRUST_TODAY": "2027-01-02"}) == date(2027, 1, 2)


def test_approval_needs_exact_value():
    assert checks.approved({"TRUST_BASELINE_APPROVED": "1"})
    assert not checks.approved({"TRUST_BASELINE_APPROVED": "true"})
    assert not checks.approved({})


class TestMetricSources:
    def test_services_to_routes_counts_contract_two_ignore_imports(self):
        assert count_services_to_routes(PYPROJECT) == 2

    def test_services_to_routes_without_contract_fails(self):
        with pytest.raises(metrics.MetricError):
            count_services_to_routes('[tool.importlinter]\nroot_packages = ["src"]\n')

    def test_services_to_routes_without_ignore_list_is_zero(self):
        text = PYPROJECT.split("ignore_imports")[0] + "\n[tool.other]\n"
        assert count_services_to_routes(text) == 0

    def test_direct_db_clients_skip_database_and_tests(self):
        assert count_direct_db_clients(PYPROJECT) == 2

    def test_broken_toml_fails(self):
        with pytest.raises(metrics.MetricError):
            count_direct_db_clients("[tool.ruff\n")

    def test_eslint_exceptions_sum_all_arrays(self):
        assert metrics.count_eslint_exceptions('{"a": ["x", "y"], "b": [], "c": ["z"]}', "x.json") == 3

    @pytest.mark.parametrize("text", ['{"a": "x"}', "[]", "{", '{"a": [1]}'])
    def test_eslint_exceptions_bad_shape_fails(self, text):
        with pytest.raises(metrics.MetricError):
            metrics.count_eslint_exceptions(text, "x.json")

    def test_ts_any_forms(self):
        text = "let a: any;\nconst b = c as any;\nconst d = <any>e;\nlet f: Array<any>;\nlet g: anything;\nlet h:any;\n"
        assert metrics.count_ts_any(text) == 5

    def test_ty_baseline_entries(self):
        assert metrics.count_ty_baseline(json.dumps(TY_BASELINE), "x.json") == 2

    @pytest.mark.parametrize("text", ["{}", '{"files": []}', '{"files": {"a.py": {}}}', "nope"])
    def test_ty_baseline_bad_shape_fails(self, text):
        with pytest.raises(metrics.MetricError):
            metrics.count_ty_baseline(text, "x.json")

    def test_knip_sums_every_issue_array(self):
        assert metrics.count_knip(KNIP_OUTPUT, "knip") == 4

    @pytest.mark.parametrize("text", ["", "not json", '{"x": []}', '{"issues": {}}'])
    def test_knip_bad_output_fails(self, text):
        with pytest.raises(metrics.MetricError):
            metrics.count_knip(text, "knip")

    def test_ci_quarantine_counts_marks_not_text(self):
        text = (
            "import pytest\n"
            "pytestmark = [pytest.mark.unit, pytest.mark.ci_quarantine]\n"
            "@pytest.mark.ci_quarantine(reason='x')\n"
            "def test_a():\n"
            "    pass\n"
            "@pytest.mark.parametrize('v', [pytest.param(1, marks=pytest.mark.ci_quarantine)])\n"
            "def test_b(v):\n"
            "    assert 'ci_quarantine'\n"
        )
        assert metrics.count_ci_quarantine(text) == 3

    def test_prose_only_rules_reads_only_its_section(self):
        assert count_prose_only_rules(STANDARDS) == 2

    def test_prose_only_rules_bad_status_fails(self):
        text = STANDARDS.replace("| 판단(사람) |", "| 아마도 |", 1)
        with pytest.raises(metrics.MetricError) as error:
            count_prose_only_rules(text)
        assert error.value.line == 11

    @pytest.mark.parametrize(
        "text",
        [
            "# 규칙\n",
            "## 규칙과 강제 수단\n\n| 가 | 나 |\n|---|---|\n",
            "## 규칙과 강제 수단\n\n| 규칙 | 출처 | 강제 수단 | 상태 |\n|---|---|---|---|\n| 가 | 나 |\n",
        ],
    )
    def test_prose_only_rules_missing_table_fails(self, text):
        with pytest.raises(metrics.MetricError):
            count_prose_only_rules(text)

    def test_comment_counts_reuse_scanner(self):
        result = scan_text(
            "a.py",
            '"""doc"""\nx = 1  # 외부: 제약\ny = 2  # noqa: F401 이유 · 만료 2027-01-01\n# 라이선스\n# https://h/o/r/issues/2\n',
        )
        assert metrics.comment_counts([result]) == {
            "allowed_comments_external": 1,
            "allowed_comments_issue": 1,
            "allowed_comments_license": 1,
            "disallowed_comments": 1,
            "suppressions": 1,
        }


class TestBaselineFile:
    def test_render_shape(self):
        text = metrics.render_baseline(EXPECTED)
        assert text.endswith("}\n")
        assert text.splitlines()[1] == '  "allowed_comments_external": 1,'
        keys = list(json.loads(text))
        assert keys == sorted(keys) == sorted(KEYS)
        assert len(KEYS) == 13

    def test_parse_valid(self):
        values, problems = metrics.parse_baseline(metrics.render_baseline(EXPECTED), "x.json", KEYS)
        assert values == EXPECTED
        assert problems == []

    @pytest.mark.parametrize(
        ("data", "needle"),
        [
            ({k: v for k, v in EXPECTED.items() if k != "ts_any"}, "ts_any"),
            ({**EXPECTED, "new_key": 1}, "new_key"),
            ({**EXPECTED, "ts_any": -1}, "ts_any"),
            ({**EXPECTED, "ts_any": True}, "ts_any"),
            ({**EXPECTED, "ts_any": 1.5}, "ts_any"),
        ],
    )
    def test_parse_rejects_bad_keys_and_values(self, data, needle):
        _, problems = metrics.parse_baseline(json.dumps(data), "x.json", KEYS)
        assert len(problems) == 1
        assert needle in problems[0].message

    def test_parse_rejects_non_object(self):
        _, problems = metrics.parse_baseline("[1]", "x.json", KEYS)
        assert len(problems) == 1


class TestMetricsCommand:
    def test_plain_prints_every_key_and_revert_ratio(self, metrics_repo, capsys):
        assert run_metrics(metrics_repo) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[:13] == [f"{key}={EXPECTED[key]} (기준선 {EXPECTED[key]})" for key in KEYS]
        assert out[13].startswith("revert_ratio_6m=")

    def test_collect_counts_sources(self, metrics_repo):
        values, problems = collect(metrics_repo)
        assert problems == []
        assert values == EXPECTED

    def test_untracked_files_are_counted(self, metrics_repo):
        write(metrics_repo, "web/src/new.ts", "let a: any;\n")
        values, _ = collect(metrics_repo)
        assert values["ts_any"] == 3

    def test_check_unchanged_passes(self, metrics_repo, capsys):
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        assert capsys.readouterr().out.splitlines()[-1] == "metrics: 문제 없음"

    def test_check_increase_fails_and_label_passes(self, metrics_repo, capsys):
        write(metrics_repo, "web/src/b.ts", "let b: any;\n")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert "ts_any=3 (기준선 2)" in out
        assert "trust-baseline.json:" in out
        assert "ts_any 가 기준선 2 보다 늘었다(현재 3) — " in out
        assert "baseline-approved" in out
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 0

    def test_check_raising_only_pr_baseline_fails(self, metrics_repo, capsys):
        write(metrics_repo, BASELINE_PATH, baseline_with(ts_any=9))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert "기준선 값 ts_any=9 가 기준 ref 값 2 보다 크다" in capsys.readouterr().out
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 0

    def test_check_compares_with_base_ref_not_worktree(self, metrics_repo, capsys):
        write(metrics_repo, "web/src/b.ts", "let b: any;\n")
        write(metrics_repo, BASELINE_PATH, baseline_with(ts_any=3))
        git(metrics_repo, "add", "-A")
        git(metrics_repo, "commit", "-q", "-m", "pr")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD~1") == 1
        out = capsys.readouterr().out
        assert "ts_any 가 기준선 2 보다 늘었다(현재 3)" in out

    def test_check_ty_baseline_growth_fails(self, metrics_repo, capsys):
        grown = json.loads(json.dumps(TY_BASELINE))
        grown["files"]["c.py"] = [{"rule": "z", "startColumn": 1, "endColumn": 2, "lineCount": 1}]
        write(metrics_repo, TY_BASELINE_PATH, json.dumps(grown))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert "app/ty-baseline.json:1: ty 기준선 진단 항목이 기준 ref 보다 늘었다(2 → 3)" in capsys.readouterr().out
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 0

    def test_check_decrease_only_notifies(self, metrics_repo, capsys):
        write(metrics_repo, "web/src/a.ts", "export const a = 1; // 설명\n")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        out = capsys.readouterr().out
        assert "알림: ts_any 가 기준선 2 보다 줄었다(현재 0)" in out
        assert out.splitlines()[-1] == "metrics: 문제 없음"

    def test_check_missing_or_unknown_key_fails(self, metrics_repo, capsys):
        data = {k: v for k, v in EXPECTED.items() if k != "knip_unused"}
        write(metrics_repo, BASELINE_PATH, json.dumps(data))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert "knip_unused" in capsys.readouterr().out
        write(metrics_repo, BASELINE_PATH, json.dumps({**EXPECTED, "extra": 0}))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        assert "extra" in capsys.readouterr().out

    def test_check_without_base_baseline_uses_worktree_and_notifies(self, fresh_metrics_repo, capsys):
        metrics_repo = fresh_metrics_repo
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        out = capsys.readouterr().out
        assert "알림: 기준 ref 에 trust-baseline.json 이 없어 작업 트리 파일과 비교한다" in out
        write(metrics_repo, BASELINE_PATH, baseline_with(suppressions=0))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert "suppressions 가 기준선 0 보다 늘었다(현재 1)" in capsys.readouterr().out

    def test_check_without_any_baseline_fails(self, fresh_metrics_repo, capsys):
        assert run_metrics(fresh_metrics_repo, "--check", "--base", "HEAD") == 1
        assert "기준선 파일이 없다 — metrics --update 로 만든다" in capsys.readouterr().out

    def test_update_writes_sorted_file(self, metrics_repo, capsys):
        write(metrics_repo, "web/src/b.ts", "let b: any;\n")
        write(metrics_repo, BASELINE_PATH, "{}\n")
        assert run_metrics(metrics_repo, "--update") == 0
        text = (metrics_repo / BASELINE_PATH).read_text(encoding="utf-8")
        assert text == metrics.render_baseline({**EXPECTED, "ts_any": 3})
        capsys.readouterr()

    def test_update_ignores_label_and_always_writes_current(self, metrics_repo, capsys):
        write(metrics_repo, "web/src/b.ts", "let b: any; let c: any;\n")
        assert run_metrics(metrics_repo, "--update", env=TODAY_ENV) == 0
        assert json.loads((metrics_repo / BASELINE_PATH).read_text(encoding="utf-8"))["ts_any"] == 4
        capsys.readouterr()

    def test_unreadable_source_fails_and_update_does_not_write(self, metrics_repo, capsys):
        (metrics_repo / "web/eslint-exceptions.json").unlink()
        assert run_metrics(metrics_repo) == 1
        out = capsys.readouterr().out
        assert "eslint_exceptions=읽지 못함" in out
        assert "web/eslint-exceptions.json:1: eslint_exceptions 를 셀 수 없다" in out
        before = (metrics_repo / BASELINE_PATH).read_text(encoding="utf-8")
        assert run_metrics(metrics_repo, "--update") == 1
        assert (metrics_repo / BASELINE_PATH).read_text(encoding="utf-8") == before
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        capsys.readouterr()

    def test_knip_failure_fails(self, metrics_repo, capsys):
        def broken(root: Path, directory: str) -> str:
            raise metrics.MetricError("web/package.json", 1, "knip 실행 실패")

        assert run(metrics_repo, "metrics", knip=broken) == 1
        assert "web/package.json:1: knip_unused 를 셀 수 없다(knip 실행 실패)" in capsys.readouterr().out

    def test_comment_scan_failure_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/src/broken.py", "def f(:\n")
        assert run_metrics(metrics_repo) == 1
        assert "app/src/broken.py:1: " in capsys.readouterr().out

    def test_check_requires_base_and_excludes_update(self, metrics_repo):
        with pytest.raises(SystemExit):
            run_metrics(metrics_repo, "--check")
        with pytest.raises(SystemExit):
            run_metrics(metrics_repo, "--base", "HEAD")
        with pytest.raises(SystemExit):
            run_metrics(metrics_repo, "--check", "--base", "HEAD", "--update")

    def test_bad_ref_fails(self, metrics_repo, capsys):
        assert run_metrics(metrics_repo, "--check", "--base", "no-such-ref") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: ")
        assert "no-such-ref" in out

    def test_revert_ratio(self, metrics_repo, capsys):
        git(metrics_repo, "checkout", "-q", "-b", "side")
        git(metrics_repo, "commit", "-q", "--allow-empty", "-m", 'Revert "x"')
        git(metrics_repo, "checkout", "-q", "main")
        git(metrics_repo, "commit", "-q", "--allow-empty", "-m", "revert: y")
        git(metrics_repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        git(metrics_repo, "commit", "-q", "--allow-empty", "-m", "not a revert")
        assert metrics.revert_ratio(metrics_repo) == (2, 1)
        assert run_metrics(metrics_repo) == 0
        assert "revert_ratio_6m=2.0000 (되돌림 2 / 머지 1)" in capsys.readouterr().out


def test_real_knip_runner_reports_missing_install(tmp_path):
    with pytest.raises(metrics.MetricError) as error:
        metrics.run_knip(tmp_path, "web")
    assert "bun install --frozen-lockfile" in error.value.message


RUFF_ACCEPTED_NOQA = [
    "import os  # noqa",
    "import os  # NOQA",
    "import os  # NoQa: F401",
    "import os  #noqa:F401",
    "import os  # foo#noqa",
    "import os  # foo # Noqa : F401",
    "import os  # 외부: 바깥 제약 # NOQA: F401",
]


class TestReviewSuppressionVariants:
    @pytest.mark.parametrize("line", RUFF_ACCEPTED_NOQA)
    def test_ruff_accepted_noqa_variants_need_reason(self, line):
        result = scan_text("x.py", line + "\n")
        assert [item.marker for item in result.suppressions] == ["noqa"]
        found = problems("x.py", line + "\n")
        assert any("이유·만료가 없다" in item for item in found)

    def test_uppercase_marker_after_allowed_comment_with_reason_passes(self):
        text = "import os  # 외부: 바깥 제약 # NOQA: F401 쓰지 않는 import · 만료 2027-01-01\n"
        result = scan_text("x.py", text)
        assert [comment.category for comment in result.comments] == [EXTERNAL]
        assert len(result.suppressions) == 1
        assert problems("x.py", text) == []

    @pytest.mark.parametrize(
        ("path", "text"),
        [
            ("x.py", "a = 1  # Type: Ignore\n"),
            ("x.py", "a = 1  # PRAGMA: NO COVER\n"),
            ("x.ts", "// ESLINT-DISABLE-NEXT-LINE\n"),
            ("x.ts", "// @TS-IGNORE\n"),
        ],
    )
    def test_other_markers_are_found_regardless_of_case(self, path, text):
        assert len(scan_text(path, text).suppressions) == 1


class TestReviewShell:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("cat <<EOF.txt\n# 본문\nEOF.txt\n# 설명\n", [4]),
            ("cat <<'END-1'\n# 본문\nEND-1\n# 설명\n", [4]),
            ("shift=1\n(( x = 1 << shift ))\n# 설명\n", [3]),
            ("y=$(( 1 << 2 ))\n# 설명\n", [2]),
            ("echo $((16#ff)) # 설명\n", [1]),
            ('echo "$(\n# 설명\necho hi\n)"\n', [2]),
            ('echo "a $(echo "#no") b" # 설명\n', [1]),
        ],
    )
    def test_comment_lines(self, text, expected):
        assert lines_of("x.sh", text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "cat <<EOF.txt\ntext\nEOF.txt\n# 설명\n",
            "shift=1\n(( x = 1 << shift ))\n# 설명\n",
            'echo "$(\n# 설명\necho hi\n)"\n',
        ],
    )
    def test_entry_point_fails(self, repo, capsys, text):
        write(repo, "app/probe.sh", text)
        assert run(repo, "comments", "--base", "HEAD") == 1
        assert capsys.readouterr().out.startswith("app/probe.sh:")


def strip_comments(path: str, text: str) -> str:
    lines = text.split("\n")
    for comment in scan_text(path, text).comments:
        line = lines[comment.line - 1]
        cut = len(line) - len(comment.body) - 1
        assert line[cut] == "#", (comment.line, line)
        lines[comment.line - 1] = line[:cut]
    return "\n".join(lines)


def assert_yaml_comments_removable(path: str, text: str) -> None:
    yaml = pytest.importorskip("yaml")
    assert list(yaml.safe_load_all(strip_comments(path, text))) == list(yaml.safe_load_all(text))


YAML_CASES = [
    "x: |2\n   text\n  # 데이터\ny: 1\n",
    "x: >-1\n  # 데이터\n z\ny: 1 # 진짜\n",
    'x: {"a":" # 데이터"}\n',
    "x: {'a':'#b', c: [\"d\":1]} # 진짜\n",
    "- run: |2\n      a\n    # 데이터\n- b # 진짜\n",
    "jobs:\n  a:\n    steps:\n      - run: |\n          # 데이터\n        # 진짜\n",
    "a: \"x # y\" # 진짜\nb: 'it''s # z' # 진짜\n",
    "a: b#c\nd: e #진짜\n",
]


class TestReviewYaml:
    @pytest.mark.parametrize(("text", "expected"), [(YAML_CASES[0], []), (YAML_CASES[2], [])])
    def test_review_examples_have_no_comment(self, text, expected):
        assert lines_of("x.yaml", text) == expected

    @pytest.mark.parametrize("text", [YAML_CASES[0], YAML_CASES[2]])
    def test_entry_point_passes(self, repo, capsys, text):
        write(repo, "probe.yaml", text)
        assert run(repo, "comments", "--base", "HEAD") == 0
        capsys.readouterr()

    @pytest.mark.parametrize("text", YAML_CASES)
    def test_removing_found_comments_keeps_pyyaml_data(self, text):
        assert_yaml_comments_removable("x.yml", text)

    def test_real_comments_are_still_found(self):
        assert lines_of("x.yml", YAML_CASES[1]) == [4]
        assert lines_of("x.yml", YAML_CASES[5]) == [6]

    def test_removing_found_comments_keeps_data_in_repo_yaml(self):
        paths = [path for path in metrics.repo_files(REPO) if scan.language_of(path) == "yaml"]
        assert paths
        for path in paths:
            assert_yaml_comments_removable(path, (REPO / path).read_text(encoding="utf-8"))


class TestReviewTsDirective:
    def test_prefix_only_is_comment(self):
        assert categories("x.ts", "/// <reference 임의 설명\nexport {};\n") == [DISALLOWED]

    def test_complete_directives_at_top_are_not_comments(self):
        text = (
            '/// <reference path="./a.d.ts" />\n'
            "/// <reference types='vite/client'/>\n"
            '/// <reference lib="es2020" />\n'
            '/// <reference no-default-lib="true"/>\n'
            '/// <amd-module name="x" />\n'
            '/// <amd-dependency path="y" />\n'
            "export {};\n"
        )
        assert lines_of("x.ts", text) == []

    @pytest.mark.parametrize(
        "text",
        [
            'export {};\n/// <reference types="vite/client" />\n',
            '/// <reference types="vite/client" /> 그리고 설명\nexport {};\n',
            '/// <reference other="x" />\nexport {};\n',
        ],
    )
    def test_directive_elsewhere_or_with_text_is_comment(self, text):
        assert categories("x.ts", text) == [DISALLOWED]


class TestReviewAboveReason:
    def test_above_line_without_format_fails_even_if_inline_is_valid(self):
        text = "# 검사 끄기: 형식 없는 일반 설명\nimport os  # noqa: F401 실제 이유 · 만료 2027-01-01\n"
        result = problems("x.py", text)
        assert len(result) == 1
        assert result[0].startswith("x.py:1: ")
        assert "'검사 끄기: <이유> · 만료 YYYY-MM-DD'" in result[0]

    def test_above_line_with_bad_date_fails(self):
        text = "# 검사 끄기: 이유 · 만료 2027-02-30\n# fmt: off\n"
        assert any(item.startswith("x.py:1: ") for item in problems("x.py", text))

    def test_valid_above_line_counts_as_allowed(self):
        text = "# 검사 끄기: 이유 · 만료 2027-01-01\nimport os  # noqa: F401 실제 이유 · 만료 2027-01-01\n"
        assert problems("x.py", text) == []
        assert categories("x.py", text) == [SUPPRESSION_REASON, SUPPRESSION]


QUARANTINE_FORMS = {
    "attribute": "import pytest\n\n\n@pytest.mark.ci_quarantine\ndef test_q():\n    assert False\n",
    "from_import": "from pytest import mark\n\n\n@mark.ci_quarantine\ndef test_q():\n    assert False\n",
    "alias_module": "import pytest as pt\n\n\n@pt.mark.ci_quarantine\ndef test_q():\n    assert False\n",
    "alias_mark": "from pytest import mark as m\n\n\n@m.ci_quarantine\ndef test_q():\n    assert False\n",
    "module_list": "import pytest\n\npytestmark = [pytest.mark.ci_quarantine]\n\n\ndef test_q():\n    assert False\n",
    "param": (
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('v', [pytest.param(1, marks=pytest.mark.ci_quarantine), 2])\n"
        "def test_q(v):\n    assert v == 2\n"
    ),
    "getattr": "import pytest\n\n\n@getattr(pytest.mark, 'ci_quarantine')\ndef test_q():\n    assert False\n",
}


class TestReviewQuarantine:
    @pytest.mark.parametrize("name", sorted(QUARANTINE_FORMS))
    def test_every_form_is_counted(self, name):
        assert metrics.count_ci_quarantine(QUARANTINE_FORMS[name]) == 1

    @pytest.mark.parametrize(
        "text",
        [
            "value = {'ci_quarantine': 1}\n",
            "text = 'pytest.mark.ci_quarantine 설명'\n",
            "import pytest\n\n\n@pytest.mark.unit\ndef test_a():\n    pass\n",
        ],
    )
    def test_non_marks_are_not_counted(self, text):
        assert metrics.count_ci_quarantine(text) == 0

    def test_count_matches_real_pytest_deselection(self, tmp_path):
        for name, text in QUARANTINE_FORMS.items():
            write(tmp_path, f"test_{name}.py", text)
        write(tmp_path, "test_clean.py", "def test_clean():\n    assert True\n")
        write(tmp_path, "pytest.ini", "[pytest]\nmarkers =\n    ci_quarantine: 격리\n")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                str(tmp_path / "pytest.ini"),
                "--rootdir",
                str(tmp_path),
                "-m",
                "not ci_quarantine",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        counted = sum(metrics.count_ci_quarantine(text) for text in QUARANTINE_FORMS.values())
        assert f"{counted} deselected" in result.stdout
        assert "2 passed" in result.stdout

    def test_repo_tests_have_no_quarantine(self):
        counts = {
            path.name: metrics.count_ci_quarantine(path.read_text(encoding="utf-8"), str(path))
            for path in (REPO / "tests").rglob("*.py")
        }
        assert counts
        assert sum(counts.values()) == 0, counts


class TestReviewBaselineDeletion:
    def test_deleting_baseline_fails_even_with_label(self, metrics_repo, capsys):
        (metrics_repo / BASELINE_PATH).unlink()
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        out = capsys.readouterr().out
        assert "trust-baseline.json:1: 기준선 파일이 없다 — 기준 ref(HEAD)에 있던 기준선을 지웠다" in out

    def test_deleting_ty_baseline_fails_even_with_label(self, metrics_repo, capsys):
        (metrics_repo / TY_BASELINE_PATH).unlink()
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        assert "app/ty-baseline.json:1: ty 기준선 파일이 없다 — " in capsys.readouterr().out

    def test_deleted_then_recreated_baseline_compares_with_last_baseline(self, metrics_repo, capsys):
        (metrics_repo / BASELINE_PATH).unlink()
        git(metrics_repo, "add", "-u")
        git(metrics_repo, "commit", "-q", "-m", "remove baseline")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        write(metrics_repo, "web/src/b.ts", "let b: any;\n")
        write(metrics_repo, BASELINE_PATH, baseline_with(ts_any=3))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert "지워진 기준선" in out
        assert "ts_any 가 기준선 2 보다 늘었다(현재 3)" in out
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 0
        (metrics_repo / "web/src/b.ts").unlink()
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()


class TestReviewExceptionSets:
    def test_swapping_import_pair_fails(self, metrics_repo, capsys):
        replace_in(
            metrics_repo, "app/pyproject.toml", "src.services.c -> src.routes.d", "src.services.e -> src.routes.f"
        )
        out = check_fails_without_label(
            metrics_repo, capsys, "import 예외 'src.services.e -> src.routes.f' 를 새로 더했다"
        )
        assert "app/pyproject.toml:" in out

    def test_removing_import_pair_passes(self, metrics_repo, capsys):
        replace_in(metrics_repo, "app/pyproject.toml", '    "src.services.c -> src.routes.d",\n', "")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_swapping_db_exception_key_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/src/z.py", "value = 1\n")
        replace_in(metrics_repo, "app/pyproject.toml", '"src/x.py" = ["TID251"]', '"src/z.py" = ["TID251"]')
        check_fails_without_label(metrics_repo, capsys, "금지 이름(TID251) 예외 'src/z.py' 를 새로 더했다")

    def test_widening_db_exception_to_glob_fails(self, metrics_repo, capsys):
        replace_in(metrics_repo, "app/pyproject.toml", '"src/x.py" = ["TID251"]', '"src/**" = ["TID251"]')
        check_fails_without_label(metrics_repo, capsys, "금지 이름(TID251) 예외 'src/**' 는 글롭이다")

    def test_db_exception_for_missing_file_fails(self, metrics_repo, capsys):
        (metrics_repo / "app/src/x.py").unlink()
        check_fails_without_label(metrics_repo, capsys, "금지 이름(TID251) 예외 'src/x.py' 의 파일이 없다")

    @pytest.mark.parametrize("code", ["TID", "TID2", "TID25", "ALL"])
    def test_prefix_code_counts_as_db_exception(self, code):
        text = PYPROJECT.replace('"main.py" = ["F401"]', f'"main.py" = ["F401", "{code}"]')
        assert count_direct_db_clients(text) == 3

    def test_prefix_code_on_new_key_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/src/z.py", "value = 1\n")
        replace_in(
            metrics_repo, "app/pyproject.toml", '"main.py" = ["F401"]', '"main.py" = ["F401"]\n"src/z.py" = ["TID"]'
        )
        check_fails_without_label(metrics_repo, capsys, "금지 이름(TID251) 예외 'src/z.py' 를 새로 더했다")

    def test_swapping_eslint_exception_fails(self, metrics_repo, capsys):
        replace_in(metrics_repo, "web/eslint-exceptions.json", "src/c.ts", "src/d.ts")
        out = check_fails_without_label(metrics_repo, capsys, "eslint 예외 (r3, src/d.ts) 를 새로 더했다")
        assert "web/eslint-exceptions.json:" in out

    def test_moving_eslint_exception_to_other_rule_fails(self, metrics_repo, capsys):
        write(
            metrics_repo,
            "web/eslint-exceptions.json",
            json.dumps({"r1": ["src/a.ts", "src/b.ts"], "r2": [], "r3": ["src/c.ts"]}),
        )
        check_fails_without_label(metrics_repo, capsys, "eslint 예외 (r1, src/b.ts) 를 새로 더했다")


class TestReviewLooseningSettings:
    def test_dropping_tid251_from_select_fails(self, metrics_repo, capsys):
        replace_in(metrics_repo, "app/pyproject.toml", 'select = ["F", "TID251"]', 'select = ["F"]')
        check_fails_without_label(metrics_repo, capsys, "ruff 가 TID251 을 고르지 않는다")

    def test_selecting_by_prefix_passes(self, metrics_repo, capsys):
        replace_in(metrics_repo, "app/pyproject.toml", 'select = ["F", "TID251"]', 'select = ["F", "TID"]')
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    @pytest.mark.parametrize(("key", "code"), [("ignore", "TID251"), ("extend-ignore", "TID251")])
    def test_ignoring_tid251_fails(self, metrics_repo, capsys, key, code):
        replace_in(
            metrics_repo,
            "app/pyproject.toml",
            'select = ["F", "TID251"]',
            f'select = ["F", "TID251"]\n{key} = ["{code}"]',
        )
        check_fails_without_label(metrics_repo, capsys, f"ruff {key} 에 '{code}' 가 있어 TID251 이 꺼진다")

    def test_removing_banned_name_fails(self, metrics_repo, capsys):
        banned = '\n[tool.ruff.lint.flake8-tidy-imports.banned-api]\n"pymongo.MongoClient" = { msg = "m" }\n"redis.Redis" = { msg = "m" }\n'
        write(metrics_repo, "app/pyproject.toml", PYPROJECT + banned)
        commit_all(metrics_repo, "banned")
        replace_in(metrics_repo, "app/pyproject.toml", '"redis.Redis" = { msg = "m" }\n', "")
        check_fails_without_label(metrics_repo, capsys, "금지 이름 'redis.Redis' 를 뺐다")

    def test_growing_ruff_exclude_fails(self, metrics_repo, capsys):
        replace_in(
            metrics_repo,
            "app/pyproject.toml",
            'select = ["F", "TID251"]',
            'select = ["F", "TID251"]\n\n[tool.ruff]\nextend-exclude = ["src/"]',
        )
        check_fails_without_label(metrics_repo, capsys, "ruff extend-exclude 에 'src/' 를 더했다")

    def test_removing_contract_fails(self, metrics_repo, capsys):
        text = PYPROJECT.replace(
            'name = "core"\ntype = "forbidden"\nsource_modules = ["core"]\nforbidden_modules = ["src"]\n',
            'name = "core"\ntype = "forbidden"\nsource_modules = ["core.bot"]\nforbidden_modules = ["src"]\n',
        )
        write(metrics_repo, "app/pyproject.toml", text)
        check_fails_without_label(metrics_repo, capsys, "import-linter 계약 'core' 의 설정이 바뀌었다")
        start = PYPROJECT.index("[[tool.importlinter.contracts]]")
        end = PYPROJECT.index("[[tool.importlinter.contracts]]", start + 1)
        write(metrics_repo, "app/pyproject.toml", PYPROJECT[:start] + PYPROJECT[end:])
        check_fails_without_label(metrics_repo, capsys, "import-linter 계약 'core' 를 지웠다")

    def test_loosening_ty_rules_fails(self, metrics_repo, capsys):
        write(
            metrics_repo,
            "app/pyproject.toml",
            PYPROJECT + '\n[tool.ty]\nbaseline = "ty-baseline.json"\n\n[tool.ty.rules]\nfoo = "error"\nbar = "warn"\n',
        )
        commit_all(metrics_repo, "ty rules")
        replace_in(metrics_repo, "app/pyproject.toml", 'foo = "error"', 'foo = "warn"')
        check_fails_without_label(metrics_repo, capsys, "ty 규칙 'foo' 수준을 error → warn 으로 낮췄다")
        replace_in(metrics_repo, "app/pyproject.toml", 'foo = "warn"', 'foo = "error"\nbaz = "ignore"')
        check_fails_without_label(metrics_repo, capsys, "ty 규칙 'baz' 수준을 기본(error) → ignore 으로 낮췄다")
        replace_in(metrics_repo, "app/pyproject.toml", 'baz = "ignore"', 'bar = "error"')
        replace_in(metrics_repo, "app/pyproject.toml", 'bar = "warn"\n', "")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_ty_override_and_baseline_path_fail(self, metrics_repo, capsys):
        write(metrics_repo, "app/pyproject.toml", PYPROJECT + '\n[tool.ty]\nbaseline = "ty-baseline.json"\n')
        commit_all(metrics_repo, "ty")
        write(
            metrics_repo,
            "app/pyproject.toml",
            PYPROJECT
            + '\n[tool.ty]\nbaseline = "ty-baseline.json"\n\n[[tool.ty.overrides]]\ninclude = ["src/**"]\n\n[tool.ty.overrides.rules]\nfoo = "ignore"\n',
        )
        check_fails_without_label(metrics_repo, capsys, "ty overrides 에서 규칙 'foo' 를 ignore 으로 낮췄다")
        write(metrics_repo, "app/pyproject.toml", PYPROJECT + '\n[tool.ty]\nbaseline = "other.json"\n')
        check_fails_without_label(metrics_repo, capsys, "ty baseline 경로를 바꿨다")

    def test_first_introduction_skips_set_comparison(self, fresh_metrics_repo, capsys):
        write(fresh_metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        replace_in(
            fresh_metrics_repo,
            "app/pyproject.toml",
            "src.services.c -> src.routes.d",
            "src.services.e -> src.routes.f",
        )
        assert run_metrics(fresh_metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()


class TestReviewEslintLevels:
    def test_warn_level_fails(self, metrics_repo, capsys):
        def lower(results):
            results["files"]["src/index.tsx"]["rules"]["no-restricted-syntax"] = [1]

        check_fails_without_label(
            metrics_repo,
            capsys,
            "web/src/index.tsx:1: eslint 규칙 no-restricted-syntax 가 error 가 아니다",
            eslint_with(lower),
        )

    def test_ignored_file_fails(self, metrics_repo, capsys):
        def ignore(results):
            results["files"]["src/index.tsx"] = {"ignored": True, "rules": {}}

        check_fails_without_label(
            metrics_repo, capsys, "web/src/index.tsx:1: eslint 가 이 파일을 보지 않는다", eslint_with(ignore)
        )

    def test_different_options_fail(self, metrics_repo, capsys):
        def weaken(results):
            results["files"]["src/a.ts"]["rules"]["no-restricted-syntax"] = [2, {"selector": "Nothing"}]

        check_fails_without_label(
            metrics_repo,
            capsys,
            "web/src/a.ts:1: eslint 규칙 no-restricted-syntax 의 설정이 src/index.tsx 와 다르다",
            eslint_with(weaken),
        )

    def test_off_outside_exceptions_fails(self, metrics_repo, capsys):
        def off(results):
            results["files"]["src/a.ts"]["rules"]["r2"] = [0]

        check_fails_without_label(metrics_repo, capsys, "eslint 규칙 r2 가 error 가 아니다", eslint_with(off))

    def test_eslint_runner_failure_fails(self, metrics_repo, capsys):
        def broken(
            root: Path, directory: str, files: list[str], rules: list[str], probes: list[dict[str, str]]
        ) -> dict:
            raise metrics.MetricError("web/eslint.config.mjs", 1, "설정 계산 실패")

        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", eslint=broken) == 1
        assert "web/eslint.config.mjs:1: eslint 설정을 확인하지 못했다(설정 계산 실패)" in capsys.readouterr().out

    def test_real_repo_eslint_rules_are_error(self, tmp_path):
        root = real_frontend(tmp_path)
        write(root, "web/src/utils/auth.ts", "import axios from 'axios';\nexport const client = axios;\n")
        write(root, "web/src/__tests__/a.test.ts", "export const a = 1;\n")
        write(root, "web/src/App.tsx", "export const App = () => null;\n")
        contract = fixture_contract()
        files = ["src/index.tsx", "src/utils/auth.ts", "src/__tests__/a.test.ts", "src/App.tsx"]
        exceptions = json.loads((root / "web/eslint-exceptions.json").read_text(encoding="utf-8"))
        rules = contract.rules(exceptions)
        results = metrics.run_eslint(root, "web", files, rules, settings.eslint_probes(files, contract))
        assert settings.eslint_level_problems("web", results["files"], exceptions, rules, contract) == []
        assert settings.eslint_contract_problems("web/eslint.config.mjs", results["probes"], exceptions, contract) == []

    def test_real_runner_reads_project_config(self, tmp_path):
        config = (
            "const rule = { create() { return {}; } };\n"
            "export default [{\n"
            "  files: ['src/**/*.ts'],\n"
            "  plugins: { app: { rules: { 'no-react19-api': rule } }, '@typescript-eslint': { rules: { 'no-restricted-imports': rule } } },\n"
            "  rules: { 'app/no-react19-api': 'error', 'no-restricted-syntax': 'warn', 'no-restricted-properties': 'error', '@typescript-eslint/no-restricted-imports': 'error' },\n"
            "}];\n"
        )
        write(tmp_path, "web/eslint.config.mjs", config)
        write(tmp_path, "web/src/index.ts", "export {};\n")
        (tmp_path / "web/node_modules").symlink_to(REPO / "node_modules", target_is_directory=True)
        results = metrics.run_eslint(tmp_path, "web", ["src/index.ts"], API_RULES, [])
        found = settings.eslint_level_problems("web", results["files"], {}, API_RULES, fixture_contract())
        assert [problem.render().split(" — ")[0] for problem in found] == [
            "web/src/index.ts:1: eslint 규칙 no-restricted-syntax 가 error 가 아니다(1)"
        ]

    def test_real_runner_without_eslint_fails(self, tmp_path):
        with pytest.raises(metrics.MetricError) as error:
            metrics.run_eslint(tmp_path, "web", ["src/index.ts"], API_RULES, [])
        assert "bun install --frozen-lockfile" in error.value.message


class TestReviewErrorOutput:
    def test_expired_with_unreadable_file_reports_error_first(self, repo, capsys):
        write(repo, "app/broken.py", "def f(:\n")
        write(repo, "app/keep.py", "import os  # noqa: F401 이유 · 만료 2026-11-01\n")
        git(repo, "add", "-A")
        assert run(repo, "expired") == 1
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("trust_checks: 오류: ")
        assert "app/broken.py" in "\n".join(out)

    def test_expired_list_has_no_error_prefix(self, repo, capsys):
        write(repo, "app/keep.py", "import os  # noqa: F401 이유 · 만료 2026-11-01\n")
        git(repo, "add", "-A")
        assert run(repo, "expired") == 1
        out = capsys.readouterr().out
        assert out.startswith("app/keep.py:1: ")
        assert "trust_checks: 오류:" not in out

    def test_argument_error_exits_one_with_prefix(self, repo, capsys):
        with pytest.raises(SystemExit) as error:
            run(repo, "comments")
        assert error.value.code == 1
        assert capsys.readouterr().out.startswith("trust_checks: 오류: ")

    def test_unexpected_exception_is_reported_as_error(self, repo, capsys, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("예기치 못함")

        monkeypatch.setattr(checks, "find_expired", boom)
        assert run(repo, "expired") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: ")
        assert "예기치 못함" in out

    def test_tool_missing_is_error(self, repo, capsys):
        replace_in(repo, "trust.toml", str(REPO / "node_modules"), "/nonexistent/node_modules")
        write(repo, "web/src/a.ts", "export {};\n")
        assert run(repo, "comments", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert out.startswith("trust_checks: 오류: ")
        assert "bun install --frozen-lockfile" in out


RUFF_SELECTION_CASES = [
    ('[tool.ruff.lint]\nselect = ["F"]\n\n[tool.ruff]\nselect = ["TID251"]\n', False),
    ('[tool.ruff]\nselect = ["TID251"]\n', True),
    ('[tool.ruff]\nselect = ["F"]\n\n[tool.ruff.lint]\nextend-select = ["TID251"]\n', True),
    ('[tool.ruff]\nextend-ignore = ["TID251"]\n\n[tool.ruff.lint]\nselect = ["TID251"]\n', False),
    ('[tool.ruff]\nignore = ["TID251"]\n\n[tool.ruff.lint]\nselect = ["TID251"]\nignore = ["F"]\n', True),
    ('[tool.ruff.lint]\nselect = ["TID251"]\nignore = ["TID"]\n', True),
    ('[tool.ruff.lint]\nselect = ["TID"]\nignore = ["TID2"]\n', False),
    ('[tool.ruff.lint]\nselect = ["ALL"]\nextend-ignore = ["TID"]\n', False),
    ('[tool.ruff.lint]\nselect = ["T"]\n', False),
    ('[tool.ruff.lint]\nselect = ["TID25"]\n', True),
    ('[tool.ruff.lint]\nselect = ["TID251"]\nignore = ["TID251"]\n', False),
    ("[tool.ruff.lint]\n", False),
]


class TestRereviewRuffPrecedence:
    @pytest.mark.parametrize(("text", "expected"), RUFF_SELECTION_CASES)
    def test_static_selection_follows_measured_ruff(self, text, expected):
        assert settings.code_enabled(tomllib.loads(text), "TID251") is expected

    @pytest.mark.parametrize(("text", "expected"), RUFF_SELECTION_CASES)
    def test_static_selection_matches_real_ruff(self, tmp_path, text, expected):
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
        assert ("(TID251)" in enabled) is expected

    @pytest.mark.parametrize(
        ("text", "counted"),
        [
            ('[tool.ruff.lint.per-file-ignores]\n"a.py" = ["T"]\n', 0),
            ('[tool.ruff.lint.per-file-ignores]\n"a.py" = ["TID"]\n', 1),
            (
                '[tool.ruff.lint.per-file-ignores]\n"b.py" = ["F"]\n\n[tool.ruff]\nper-file-ignores = { "a.py" = ["TID251"] }\n',
                0,
            ),
            ('[tool.ruff]\nper-file-ignores = { "a.py" = ["TID251"] }\n', 1),
            ('[tool.ruff.lint.extend-per-file-ignores]\n"a.py" = ["TID251"]\n', 1),
        ],
    )
    def test_db_exceptions_follow_precedence(self, text, counted):
        assert len(settings.code_exceptions(tomllib.loads(text), "TID251")) == counted

    def test_ignored_top_select_fails(self, metrics_repo, capsys):
        replace_in(metrics_repo, "app/pyproject.toml", 'select = ["F", "TID251"]', 'select = ["F"]')
        write(
            metrics_repo,
            "app/pyproject.toml",
            (metrics_repo / "app/pyproject.toml").read_text() + '\n[tool.ruff]\nselect = ["TID251"]\n',
        )
        check_fails_without_label(metrics_repo, capsys, "ruff 가 TID251 을 고르지 않는다")

    def test_top_extend_ignore_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/pyproject.toml", PYPROJECT + '\n[tool.ruff]\nextend-ignore = ["TID251"]\n')
        check_fails_without_label(metrics_repo, capsys, "ruff extend-ignore 에 'TID251' 가 있어 TID251 이 꺼진다")

    def test_broader_ignore_than_select_passes(self, metrics_repo, capsys):
        replace_in(
            metrics_repo,
            "app/pyproject.toml",
            'select = ["F", "TID251"]',
            'select = ["F", "TID251"]\nignore = ["TID"]',
        )
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_ruff_extend_change_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/pyproject.toml", PYPROJECT + '\n[tool.ruff]\nextend = "other.toml"\n')
        check_fails_without_label(metrics_repo, capsys, "ruff extend 를 바꿨다")


class TestRereviewEslintContract:
    def test_real_config_satisfies_contract(self, tmp_path):
        assert contract_problems(real_frontend(tmp_path)) == []

    @pytest.mark.parametrize(
        ("old", "new", "needle"),
        [
            ("object: 'Promise'", "object: 'NeverPromise'", "Promise.withResolvers"),
            ("name: 'axios'", "name: 'never-axios'", "axios"),
            ("'useOptimistic'", "'neverUseOptimistic'", "react 의 useOptimistic"),
            ('Identifier[name="WebSocket"]', 'Identifier[name="NeverSocket"]', "new WebSocket"),
        ],
    )
    def test_weakened_global_options_fail(self, tmp_path, old, new, needle):
        root = real_frontend(tmp_path)
        replace_in(root, "web/eslint.config.mjs", old, new)
        found = contract_problems(root)
        assert found
        assert all(line.startswith("web/eslint.config.mjs:") for line in found)
        assert any(f"eslint 금지 대상 '{needle}" in line for line in found), found

    def test_missing_probe_message_fails_without_label(self, metrics_repo, capsys):
        def drop(results):
            for probe in results["probes"]:
                probe["messages"] = [m for m in probe["messages"] if m["ruleId"] != "no-restricted-properties"]

        check_fails_without_label(
            metrics_repo, capsys, "eslint 금지 대상 'Promise.withResolvers' 가 막히지 않는다", eslint_with(drop)
        )

    def test_ignored_probe_fails(self, metrics_repo, capsys):
        def ignore(results):
            for probe in results["probes"]:
                probe["ignored"] = True
                probe["messages"] = []

        check_fails_without_label(metrics_repo, capsys, "eslint 가 검사용 파일", eslint_with(ignore))


def ty_rules_pyproject(rules: str) -> str:
    return PYPROJECT + '\n[tool.ty]\nbaseline = "ty-baseline.json"\n' + (f"\n[tool.ty.rules]\n{rules}" if rules else "")


class TestRereviewTyDefaults:
    def test_defaults_table_shape(self):
        table = settings.load_ty_defaults()
        assert table["version"]
        assert table["rules"]["invalid-assignment"] == "error"
        assert set(table["rules"].values()) <= {"error", "warn", "ignore"}
        assert len(table["rules"]) > 100

    def test_defaults_table_matches_real_ty(self):
        table = settings.load_ty_defaults()
        ty = require_tool("ty", table["version"])
        explain = subprocess.run([ty, "explain", "rule"], capture_output=True, text=True, check=True).stdout
        assert settings.parse_ty_explain(explain) == table["rules"]
        rendered = settings.render_ty_defaults(table["version"], explain)
        assert rendered == settings.TY_DEFAULTS_PATH.read_text(encoding="utf-8")

    def test_explicit_default_error_passes(self, metrics_repo, capsys):
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(""))
        commit_all(metrics_repo, "ty")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject('invalid-assignment = "error"\n'))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_lowering_default_error_fails(self, metrics_repo, capsys):
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(""))
        commit_all(metrics_repo, "ty")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject('invalid-assignment = "warn"\n'))
        check_fails_without_label(
            metrics_repo, capsys, "ty 규칙 'invalid-assignment' 수준을 기본(error) → warn 으로 낮췄다"
        )

    def test_default_warn_rule(self, metrics_repo, capsys):
        rule = next(name for name, level in settings.load_ty_defaults()["rules"].items() if level == "warn")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(""))
        commit_all(metrics_repo, "ty")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(f'{rule} = "warn"\n'))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(f'{rule} = "ignore"\n'))
        check_fails_without_label(metrics_repo, capsys, f"ty 규칙 '{rule}' 수준을 기본(warn) → ignore 으로 낮췄다")

    def test_raising_default_ignore_rule_passes(self, metrics_repo, capsys):
        rule = next(name for name, level in settings.load_ty_defaults()["rules"].items() if level == "ignore")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(""))
        commit_all(metrics_repo, "ty")
        write(metrics_repo, "app/pyproject.toml", ty_rules_pyproject(f'{rule} = "warn"\n'))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_parse_ty_explain(self):
        text = "# a-rule\n\nDefault level: error | Stable\n\n## What\n# b-rule\nDefault level: warn · Preview\n"
        assert settings.parse_ty_explain(text) == {"a-rule": "error", "b-rule": "warn"}


class TestRereviewOtherConfigFiles:
    @pytest.mark.parametrize(
        "path",
        [
            "app/ruff.toml",
            "app/.ruff.toml",
            "app/src/tasks/ruff.toml",
            "app/ty.toml",
            "app/.importlinter",
            "app/setup.cfg",
            "app/tox.ini",
            "app/.flake8",
        ],
    )
    def test_tracked_tool_config_under_backend_fails(self, repo, capsys, path):
        write(repo, path, '[lint]\nselect = ["F"]\n')
        git(repo, "add", path)
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out
        assert out.startswith(f"{path}:1: ")
        assert "app/pyproject.toml 한 곳" in out

    def test_tool_config_outside_backend_passes(self, repo, capsys):
        write(repo, "web/setup.cfg", "[x]\n")
        git(repo, "add", "-A")
        assert run(repo, "repo") == 0
        capsys.readouterr()

    def test_nested_pyproject_with_tool_section_fails(self, repo, capsys):
        write(repo, "app/rust/x/pyproject.toml", '[tool.maturin]\nname = "x"\n')
        git(repo, "add", "-A")
        assert run(repo, "repo") == 0
        write(repo, "app/rust/x/pyproject.toml", '[tool.maturin]\nname = "x"\n\n[tool.ruff.lint]\nselect = ["F"]\n')
        git(repo, "add", "-A")
        assert run(repo, "repo") == 1
        assert "app/rust/x/pyproject.toml:1: " in capsys.readouterr().out

    def test_importlinter_top_level_key_change_fails(self, metrics_repo, capsys):
        replace_in(
            metrics_repo,
            "app/pyproject.toml",
            'root_packages = ["core", "src"]',
            'root_packages = ["core", "src"]\nexclude_type_checking_imports = true',
        )
        check_fails_without_label(
            metrics_repo, capsys, "import-linter 설정 'exclude_type_checking_imports' 가 바뀌었다"
        )


class TestRereviewTyBaselineHistory:
    def test_ty_baseline_deleted_at_base_is_recovered(self, metrics_repo, capsys):
        (metrics_repo / TY_BASELINE_PATH).unlink()
        commit_all(metrics_repo, "drop ty baseline")
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD", env=APPROVED_ENV) == 1
        assert "ty 기준선 파일이 없다 — 지워진 ty 기준선을 git show" in capsys.readouterr().out
        grown = json.loads(json.dumps(TY_BASELINE))
        grown["files"]["c.py"] = [{"rule": "z", "startColumn": 1, "endColumn": 2, "lineCount": 1}]
        write(metrics_repo, TY_BASELINE_PATH, json.dumps(grown))
        write(metrics_repo, BASELINE_PATH, baseline_with(ty_baseline_entries=3))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 1
        out = capsys.readouterr().out
        assert "지워진 ty 기준선" in out
        assert "ty 기준선 진단 항목이 기준 ref 보다 늘었다(2 → 3)" in out
        write(metrics_repo, TY_BASELINE_PATH, json.dumps(TY_BASELINE))
        write(metrics_repo, BASELINE_PATH, metrics.render_baseline(EXPECTED))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()


class TestFinalToolDiscovery:
    def test_other_version_on_path_is_not_used(self, tmp_path):
        fake = tmp_path / "ty"
        fake.write_text("#!/bin/sh\necho 'ty 0.0.49 (fake)'\n", encoding="utf-8")
        fake.chmod(0o755)
        assert find_tool("ty", "0.0.104", [str(fake)]) is None
        assert find_tool("ty", "0.0.49", [str(fake)]) == str(fake)

    def test_missing_candidates_are_skipped(self, tmp_path):
        assert find_tool("ty", "0.0.104", [None, str(tmp_path / "nothing")]) is None


def ty_scope_pyproject(global_rules: str = "", override_rules: str | None = None) -> str:
    text = PYPROJECT + '\n[tool.ty]\nbaseline = "ty-baseline.json"\n'
    if global_rules:
        text += f"\n[tool.ty.rules]\n{global_rules}"
    if override_rules is not None:
        text += f'\n[[tool.ty.overrides]]\ninclude = ["src/**"]\n\n[tool.ty.overrides.rules]\n{override_rules}'
    return text


class TestFinalTyEffectiveLevels:
    def commit_base(self, root: Path, text: str) -> None:
        write(root, "app/pyproject.toml", text)
        commit_all(root, "ty base")

    def test_global_error_on_default_ignore_rule_lowered_fails(self, metrics_repo, capsys):
        assert settings.ty_default_level("division-by-zero") == "ignore"
        self.commit_base(metrics_repo, ty_scope_pyproject('division-by-zero = "error"\n'))
        write(metrics_repo, "app/pyproject.toml", ty_scope_pyproject('division-by-zero = "ignore"\n'))
        check_fails_without_label(metrics_repo, capsys, "ty 규칙 'division-by-zero' 수준을 error → ignore 으로 낮췄다")

    def test_removing_global_error_on_default_ignore_rule_fails(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject('division-by-zero = "error"\n'))
        write(metrics_repo, "app/pyproject.toml", ty_scope_pyproject())
        check_fails_without_label(
            metrics_repo, capsys, "ty 규칙 'division-by-zero' 수준을 error → 기본(ignore) 으로 낮췄다"
        )

    def test_override_error_on_default_ignore_rule_lowered_fails(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject(override_rules='division-by-zero = "error"\n'))
        write(metrics_repo, "app/pyproject.toml", ty_scope_pyproject(override_rules='division-by-zero = "ignore"\n'))
        check_fails_without_label(
            metrics_repo, capsys, "ty overrides 에서 규칙 'division-by-zero' 를 ignore 으로 낮췄다(이전 error"
        )

    def test_removing_override_falls_back_to_global(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject(override_rules='division-by-zero = "error"\n'))
        write(metrics_repo, "app/pyproject.toml", ty_scope_pyproject())
        check_fails_without_label(
            metrics_repo, capsys, "ty overrides 에서 규칙 'division-by-zero' 를 기본(ignore) 으로 낮췄다(이전 error"
        )

    def test_override_matching_global_error_is_not_loosening(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject('division-by-zero = "error"\n'))
        write(
            metrics_repo,
            "app/pyproject.toml",
            ty_scope_pyproject('division-by-zero = "error"\n', 'division-by-zero = "error"\n'),
        )
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_override_below_global_is_loosening(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject('division-by-zero = "error"\n'))
        write(
            metrics_repo,
            "app/pyproject.toml",
            ty_scope_pyproject('division-by-zero = "error"\n', 'division-by-zero = "warn"\n'),
        )
        check_fails_without_label(
            metrics_repo, capsys, "ty overrides 에서 규칙 'division-by-zero' 를 warn 으로 낮췄다(이전 error"
        )

    def test_raising_levels_passes(self, metrics_repo, capsys):
        self.commit_base(metrics_repo, ty_scope_pyproject(override_rules='division-by-zero = "warn"\n'))
        write(metrics_repo, "app/pyproject.toml", ty_scope_pyproject(override_rules='division-by-zero = "error"\n'))
        assert run_metrics(metrics_repo, "--check", "--base", "HEAD") == 0
        capsys.readouterr()

    def test_loosening_function_uses_base_effective_level(self):
        base = tomllib.loads(ty_scope_pyproject(override_rules='division-by-zero = "error"\n'))
        work = tomllib.loads(ty_scope_pyproject(override_rules='division-by-zero = "ignore"\n'))
        assert settings._ty_override_loosening(base, work)
        assert not settings._ty_override_loosening(work, base)


class TestFinalIgnoreFiles:
    @pytest.mark.parametrize("path", [".ignore", ".rgignore", "app/.ignore", "web/src/.rgignore"])
    def test_tracked_ignore_file_fails(self, repo, capsys, path):
        write(repo, path, "src/zz.py\n")
        git(repo, "add", path)
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out
        assert out.startswith(f"{path}:1: ")
        assert "검사 도구가 건너뛴다" in out

    def test_tracked_target_file_in_gitignore_fails(self, repo, capsys):
        write(repo, "app/src/zz_review_db.py", "from pymongo import MongoClient\nMongoClient()\n")
        git(repo, "add", "-A")
        write(repo, "app/.gitignore", "src/zz_review_db.py\n")
        git(repo, "add", "app/.gitignore")
        assert run(repo, "repo") == 1
        out = capsys.readouterr().out
        assert out.startswith(
            "app/src/zz_review_db.py:1: 추적 파일을 무시 목록에 넣으면 검사 도구가 건너뛴다 — 무시 목록에서 뺀다"
        )

    def test_tracked_non_target_file_in_gitignore_passes(self, repo, capsys):
        write(repo, "data/spec.json", "{}\n")
        git(repo, "add", "-A")
        write(repo, ".gitignore", "data/spec.json\n")
        git(repo, "add", ".gitignore")
        assert run(repo, "repo") == 0
        capsys.readouterr()
