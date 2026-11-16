from pathlib import Path

import pytest
from support import (
    COMMENTS_TOML,
    EXPECTED,
    GATES,
    MOVED_BODY,
    BASELINE_PATH,
    commit_all,
    git,
    init_repo,
    write,
    write_sources,
)

from trust_checks import metrics


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "repo")
    write(root, "trust.toml", COMMENTS_TOML)
    write(root, "app/old.py", "x = 1  # 오래된 주석\n")
    write(root, "app/keep.py", "y = 1  # 손대지 않은 파일의 주석\n")
    write(root, "app/gone.py", "z = 1  # 지울 파일\n")
    write(root, "app/move_me.py", MOVED_BODY + "import os  # noqa: F401 처음 · 만료 2026-12-01\n")
    write(root, f"{GATES}/a_eval_a1.py", "a = 1\n")
    write(root, "docs/a.md", "# 제목\n")
    commit_all(root, "base")
    return root


@pytest.fixture
def metrics_repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "metrics")
    write_sources(root)
    write(root, BASELINE_PATH, metrics.render_baseline(EXPECTED))
    commit_all(root, "base")
    return root


@pytest.fixture
def fresh_metrics_repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "fresh")
    write_sources(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "sources without baseline")
    return root
