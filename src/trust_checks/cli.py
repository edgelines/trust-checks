from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from scripts.trust import checks, metrics
from scripts.trust.checks import CheckError, Problem
from scripts.trust.gitfiles import GitError
from scripts.trust.metrics import EslintRunner, KnipRunner
from scripts.trust.scan import REPO_ROOT, ToolMissing


ERROR_PREFIX = "trust_checks: 오류: "
KNOWN_ERRORS = (GitError, ToolMissing, ValueError)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(f"{ERROR_PREFIX}{message} — python -m scripts.trust_checks --help 로 사용법을 본다")
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m scripts.trust_checks",
        description="AoX 신뢰 검사: 주석 금지·검사 끄기 표시 만료·기준 숫자·저장소 소형 검사",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    comments = commands.add_parser("comments", help="바뀐 파일의 주석·검사 끄기 표시를 검사한다")
    source = comments.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", metavar="REF", help="이 git ref 와의 merge-base 대비 바뀐 파일(작업 트리 포함)")
    source.add_argument("--staged", action="store_true", help="스테이징한 파일")
    commands.add_parser("expired", help="저장소 전체에서 만료가 지난 검사 끄기 표시를 찾는다")
    commands.add_parser("repo", help="추적 파일 중 CLAUDE.md 와 .disabled 를 찾는다")
    numbers = commands.add_parser("metrics", help="기준 숫자를 세고 기준선과 비교하거나 다시 쓴다")
    mode = numbers.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="--base 의 기준선보다 늘었으면 실패한다")
    mode.add_argument(
        "--update", action="store_true", help="지금 숫자로 backend/scripts/trust_baseline.json 을 다시 쓴다"
    )
    numbers.add_argument("--base", metavar="REF", help="--check 가 비교할 기준 git ref")
    return parser


Output = tuple[int, list[str]]


def _report(problems: Sequence[Problem], success: str, failure: str) -> Output:
    lines = [problem.render() for problem in problems]
    lines.append(failure.format(count=len(problems)) if problems else success)
    return (1 if problems else 0), lines


def _metrics(
    args: argparse.Namespace, root: Path, env: Mapping[str, str], knip: KnipRunner, eslint: EslintRunner
) -> Output:
    current, problems = metrics.collect(root, knip)
    if args.check:
        result = metrics.check(root, args.base, current, checks.approved(env), eslint)
        lines = metrics.metric_lines(current, result.compared)
        notices = result.notices
        problems = [*problems, *result.problems]
    else:
        lines = metrics.metric_lines(current, metrics.worktree_baseline(root))
        notices = []
    output = [*lines, metrics.revert_line(root), *notices]
    if args.update and not problems:
        written = metrics.write_baseline(root, {key: value for key, value in current.items() if value is not None})
        return 0, [*output, f"metrics: {written.relative_to(root).as_posix()} 를 지금 숫자로 다시 썼다"]
    code, tail = _report(problems, "metrics: 문제 없음", "metrics: 문제 {count}개")
    return code, [*output, *tail]


def _dispatch(
    args: argparse.Namespace, root: Path, env: Mapping[str, str], knip: KnipRunner, eslint: EslintRunner
) -> Output:
    if args.command == "metrics":
        return _metrics(args, root, env, knip, eslint)
    now = checks.today(env)
    if args.command == "comments":
        is_approved = checks.approved(env)
        if args.staged:
            count, problems = checks.check_comments_staged(root, now, is_approved)
        else:
            count, problems = checks.check_comments_since(root, args.base, now, is_approved)
        return _report(
            problems,
            f"comments: 바뀐 대상 파일 {count}개, 문제 없음",
            f"comments: 바뀐 대상 파일 {count}개, 문제 {{count}}개",
        )
    if args.command == "expired":
        return _report(
            checks.find_expired(root, now),
            "expired: 만료가 지난 검사 끄기 표시 없음",
            "expired: 만료가 지난 검사 끄기 표시 {count}개",
        )
    return _report(checks.check_repo(root), "repo: 문제 없음", "repo: 문제 {count}개")


def run(
    argv: Sequence[str] | None = None,
    root: Path = REPO_ROOT,
    env: Mapping[str, str] = os.environ,
    knip: KnipRunner = metrics.run_knip,
    eslint: EslintRunner = metrics.run_eslint,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "metrics" and args.check != bool(args.base):
        parser.error("metrics --check 와 --base REF 는 함께 쓴다")
    try:
        code, lines = _dispatch(args, root, env, knip, eslint)
    except CheckError as error:
        code, lines = 1, [f"{ERROR_PREFIX}{error.message}", *error.details]
    except KNOWN_ERRORS as error:
        code, lines = 1, [f"{ERROR_PREFIX}{error}"]
    except Exception as error:
        code, lines = 1, [f"{ERROR_PREFIX}예기치 못한 오류 {type(error).__name__}: {error}"]
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(run())
