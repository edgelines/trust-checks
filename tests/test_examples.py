import ast
import json
import os
from pathlib import Path

import pytest
from support import REPO

from trust_checks import cli, config, settings
from trust_checks.scan import is_excluded

EXAMPLE = REPO / "examples/aox3.trust.toml"
RUFF_EXAMPLE = REPO / "examples/aox3.ruff-rules.trust.toml"
CONTRACT = REPO / "examples/aox3.eslint-contract.json"
AOX3_KEYS = {
    "services_to_routes_imports",
    "direct_db_clients",
    "frontend_api_exceptions",
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
}
GATES = "backend/src/services/trading/gates"


def example():
    return config.load(EXAMPLE, "examples/aox3.trust.toml")


def test_example_keys_match_aox3_baseline_keys():
    assert set(example().metric_keys()) == AOX3_KEYS
    assert len(example().metric_keys()) == 13


def test_ruff_example_extends_aox3_metric_keys():
    text = EXAMPLE.read_text(encoding="utf-8") + "\n" + RUFF_EXAMPLE.read_text(encoding="utf-8")
    configured = config.parse(text, "aox3 예시")
    assert set(configured.metric_keys()) == AOX3_KEYS | {"broad_except", "print_calls", "naive_now", "local_imports"}
    assert configured.allow_names == (".env.example",)


def test_example_contract_parses():
    contract = settings.parse_contract(CONTRACT.read_text(encoding="utf-8"))
    assert len(contract.expect) == 7
    assert contract.probe_text.count("\n") == 9


def aox3_root() -> Path:
    value = os.environ.get("TRUST_CHECKS_AOX3")
    if not value:
        pytest.skip("TRUST_CHECKS_AOX3 에 aox3 체크아웃 경로를 주면 예시 설정을 실제 저장소로 대조한다")
    return Path(value)


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} 을 찾지 못했다")


def test_ir_inputs_are_read_from_known_places():
    root = aox3_root()
    tree = _module_tree(root / "backend/scripts/a_gate_ir.py")
    source = ast.unparse(tree)
    assert "gen.OUT_DIR / f'{module}.py'" in source
    assert "f'a_eval_{builder.rule.rule.lower()}'" in source
    assert "gen.load_rule_body(rule)" in source
    gen_tree = _module_tree(root / "backend/scripts/gen_a_rule_eval.py")
    assert ast.unparse(_assigned(gen_tree, "OUT_DIR")) == "REPO_ROOT / 'backend/src/services/trading/gates'"
    assert ast.unparse(_assigned(gen_tree, "RULE_DOC_DIR")) == "OUT_DIR / 'rules'"


def test_exclusion_list_covers_every_ir_input():
    root = aox3_root()
    exclude = example().exclude
    tree = _module_tree(root / "backend/scripts/a_gate_ir.py")
    scope = ast.literal_eval(_assigned(tree, "_SCOPE_MODULES"))
    gates = root / GATES
    inputs = [f"{GATES}/{module}.py" for module in scope]
    inputs += [f"{GATES}/{path.name}" for path in gates.glob("a_eval_*.py")]
    inputs += [path.relative_to(root).as_posix() for path in (gates / "rules").rglob("*") if path.is_file()]
    assert len(inputs) > 100
    assert [path for path in inputs if not is_excluded(path, exclude)] == []


def test_example_metrics_equal_aox3_baseline(capsys):
    root = aox3_root()
    baseline = json.loads((root / "backend/scripts/trust_baseline.json").read_text(encoding="utf-8"))
    assert cli.run(["--root", str(root), "--config", str(EXAMPLE), "metrics"], env={"TRUST_TODAY": "2026-11-16"}) == 0
    lines = capsys.readouterr().out.splitlines()
    found = dict(line.split(" ", 1)[0].split("=", 1) for line in lines[:13])
    assert {key: int(value) for key, value in found.items()} == baseline
