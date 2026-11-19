from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from trust_checks import checks, metrics
from trust_checks.checks import CheckError, Problem
from trust_checks.config import CONFIG_NAME, Config, ConfigError, load
from trust_checks.gitfiles import GitError, git
from trust_checks.metrics import EslintRunner, KnipRunner, RuffDiagnostic, RuffRunner
from trust_checks.scan import ToolMissing

ERROR_PREFIX = "trust_checks: 오류: "
KNOWN_ERRORS = (GitError, ToolMissing, ConfigError, ValueError)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(f"{ERROR_PREFIX}{message} — trust-checks --help 로 사용법을 본다")
        raise SystemExit(1)


def _common() -> _Parser:
    common = _Parser(add_help=False)
    common.add_argument(
        "--root", metavar="PATH", default=argparse.SUPPRESS, help="검사할 저장소 뿌리(기본: 현재 폴더의 git 뿌리)"
    )
    common.add_argument(
        "--config", metavar="PATH", default=argparse.SUPPRESS, help=f"설정 파일(기본: <root>/{CONFIG_NAME})"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = _Parser(
        prog="trust-checks",
        description="신뢰 검사: 주석 금지·검사 끄기 표시 만료·기준 숫자·저장소 소형 검사",
        parents=[common],
    )
    commands = parser.add_subparsers(dest="command", required=True)
    comments = commands.add_parser("comments", help="바뀐 파일의 주석·검사 끄기 표시를 검사한다", parents=[common])
    source = comments.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", metavar="REF", help="이 git ref 와의 merge-base 대비 바뀐 파일(작업 트리 포함)")
    source.add_argument("--staged", action="store_true", help="스테이징한 파일")
    commands.add_parser("expired", help="저장소 전체에서 만료가 지난 검사 끄기 표시를 찾는다", parents=[common])
    commands.add_parser("repo", help="추적하면 안 되는 파일·별도 도구 설정·무시 목록을 찾는다", parents=[common])
    numbers = commands.add_parser("metrics", help="기준 숫자를 세고 기준선과 비교하거나 다시 쓴다", parents=[common])
    mode = numbers.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="--base 의 기준선보다 늘었으면 실패한다")
    mode.add_argument("--update", action="store_true", help="지금 숫자로 기준선 파일([trust] baseline)을 다시 쓴다")
    numbers.add_argument("--base", metavar="REF", help="--check 가 비교할 기준 git ref")
    return parser


Output = tuple[int, list[str]]


def _report(problems: Sequence[Problem], success: str, failure: str) -> Output:
    lines = [problem.render() for problem in problems]
    lines.append(failure.format(count=len(problems)) if problems else success)
    return (1 if problems else 0), lines


def resolve_root(value: str | None, cwd: Path) -> Path:
    if value:
        path = (cwd / value).resolve()
        if not path.is_dir():
            raise ConfigError(f"--root {value} 폴더가 없다 — 있는 저장소 경로를 준다")
        try:
            top = Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        except GitError:
            raise ConfigError(f"--root {value} 가 git 저장소가 아니다 — 저장소 뿌리를 준다") from None
        if top != path:
            raise ConfigError(f"--root {value} 는 저장소 뿌리가 아니다 — 뿌리({top})를 준다")
        return path
    try:
        top = git(cwd, "rev-parse", "--show-toplevel").decode().strip()
    except GitError:
        raise ConfigError("현재 폴더가 git 저장소가 아니다 — 저장소 안에서 돌리거나 --root 로 경로를 준다") from None
    return Path(top)


def resolve_config(value: str | None, root: Path, cwd: Path) -> Config:
    path = (cwd / value).resolve() if value else root / CONFIG_NAME
    try:
        relative: str | None = path.relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = None
    label = relative if relative is not None else (value or str(path))
    return load(path, label, relative, root)


def _metrics(
    args: argparse.Namespace,
    root: Path,
    config: Config,
    env: Mapping[str, str],
    knip: KnipRunner,
    eslint: EslintRunner,
    ruff: RuffRunner,
) -> Output:
    keys = config.metric_keys()
    ruff_details: dict[str, list[RuffDiagnostic]] = {}
    current, problems = metrics.collect(root, config, knip, ruff, ruff_details if args.check else None)
    if args.check:
        result = metrics.check(root, config, args.base, current, checks.approved(env), eslint, ruff_details)
        lines = metrics.metric_lines(keys, current, result.compared)
        notices = result.notices
        problems = [*problems, *result.problems]
    else:
        lines = metrics.metric_lines(keys, current, metrics.worktree_baseline(root, config))
        notices = []
    output = [*lines, metrics.revert_line(root), *notices]
    if args.update and not problems:
        values = {key: value for key, value in current.items() if value is not None}
        written = metrics.write_baseline(root, config, values)
        return 0, [*output, f"metrics: {written.relative_to(root).as_posix()} 를 지금 숫자로 다시 썼다"]
    code, tail = _report(problems, "metrics: 문제 없음", "metrics: 문제 {count}개")
    return code, [*output, *tail]


def _dispatch(
    args: argparse.Namespace,
    cwd: Path,
    env: Mapping[str, str],
    knip: KnipRunner,
    eslint: EslintRunner,
    ruff: RuffRunner,
) -> Output:
    root = resolve_root(getattr(args, "root", None), cwd)
    config = resolve_config(getattr(args, "config", None), root, cwd)
    if args.command == "metrics":
        return _metrics(args, root, config, env, knip, eslint, ruff)
    now = checks.today(env)
    if args.command == "comments":
        is_approved = checks.approved(env)
        if args.staged:
            count, problems = checks.check_comments_staged(root, config, now, is_approved)
        else:
            count, problems = checks.check_comments_since(root, config, args.base, now, is_approved)
        return _report(
            problems,
            f"comments: 바뀐 대상 파일 {count}개, 문제 없음",
            f"comments: 바뀐 대상 파일 {count}개, 문제 {{count}}개",
        )
    if args.command == "expired":
        return _report(
            checks.find_expired(root, config, now),
            "expired: 만료가 지난 검사 끄기 표시 없음",
            "expired: 만료가 지난 검사 끄기 표시 {count}개",
        )
    return _report(checks.check_repo(root, config), "repo: 문제 없음", "repo: 문제 {count}개")


def run(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] = os.environ,
    cwd: Path | None = None,
    knip: KnipRunner = metrics.run_knip,
    eslint: EslintRunner = metrics.run_eslint,
    ruff: RuffRunner = metrics.run_ruff,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "metrics" and args.check != bool(args.base):
        parser.error("metrics --check 와 --base REF 는 함께 쓴다")
    try:
        code, lines = _dispatch(args, cwd or Path.cwd(), env, knip, eslint, ruff)
    except CheckError as error:
        code, lines = 1, [f"{ERROR_PREFIX}{error.message}", *error.details]
    except KNOWN_ERRORS as error:
        code, lines = 1, [f"{ERROR_PREFIX}{error}"]
    except Exception as error:
        code, lines = 1, [f"{ERROR_PREFIX}예기치 못한 오류 {type(error).__name__}: {error}"]
    print("\n".join(lines))
    return code


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
