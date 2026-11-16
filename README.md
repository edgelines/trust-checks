# trust-checks

에이전트가 고친 코드를 사람이 다 읽지 않아도 믿을 수 있게, **어기면 실패하는 검사**를 저장소마다 같은 방식으로 돌리는 공용 도구다.

- 주석 99% 금지(허용 머리말 셋 + 검사 끄기 표시)
- 검사 끄기 표시의 이유·만료 형식과 만료 검사
- 기준 숫자(지표) 기록과 증가 차단 — 기준 ref 의 기준선과 비교한다
- 저장소 소형 검사(추적하면 안 되는 파일, 별도 도구 설정, 무시 목록)

저장소마다 뿌리에 `trust.toml` 하나를 두고, 기본 기능에 더해 필요한 선택 지표를 켠다. 설치 의존성은 없다(Python 표준 라이브러리만). TS/JS 파일 판정과 eslint 검사에만 `bun` 과 저장소의 `node_modules` 를 쓴다.

## 설치·실행

태그로 고정한 판을 `uvx` 로 부른다. 저장소 뿌리에서 돌린다.

```sh
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks comments --base origin/main
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks comments --staged
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks expired
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks repo
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks metrics
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks metrics --check --base origin/main
uvx --from git+https://github.com/edgelines/trust-checks@v0.1.1 trust-checks metrics --update
```

필요한 것:

- Python 3.13 이상, git
- TS/JS 대상 파일이 있으면 `bun` 과 typescript 가 설치된 `node_modules`(`[trust] node_modules`). 먼저 `bun install --frozen-lockfile` 을 한다
- `[metrics.eslint]`·`[metrics.knip]` 을 켜면 그 폴더(또는 위 폴더)의 `node_modules` 에 eslint·knip

## 명령

| 명령 | 하는 일 |
|---|---|
| `comments --base <ref>` | `<ref>` 와의 merge-base 대비 추가·수정·이름 바뀐 대상 파일(추적 안 한 새 파일 포함)을 검사한다 |
| `comments --staged` | 스테이징한 내용만 검사한다(커밋 전 훅용) |
| `expired` | 추적 파일 전체에서 만료가 지난 검사 끄기 표시를 찾는다 |
| `repo` | 저장소 소형 검사 |
| `metrics` | 지표를 세어 작업 트리 기준선과 함께 출력한다 |
| `metrics --check --base <ref>` | 지표·기준선·설정 완화를 기준 ref 와 비교한다 |
| `metrics --update` | 지금 숫자로 기준선 파일을 다시 쓴다(표지와 무관) |

공통 옵션(명령 앞뒤 어디든):

- `--root <경로>`: 검사할 저장소 뿌리(git 뿌리여야 한다. 하위 폴더를 주면 실패). 기본은 현재 폴더의 git 뿌리
- `--config <경로>`: 설정 파일. 기본은 `<root>/trust.toml`. 상대 경로는 현재 폴더 기준이다

환경 변수:

- `TRUST_BASELINE_APPROVED=1`: PR 에 사람이 `baseline-approved` 표지를 달았다는 뜻. 값이 정확히 `1` 일 때만 켜진다. CI 가 PR 표지를 읽어 넣는다
- `TRUST_TODAY=YYYY-MM-DD`: 오늘 날짜를 덮어쓴다(시험용). 없으면 한국 시각 오늘

## 출력·종료 코드 규약

워크플로가 이 글자를 그대로 읽으므로 바꾸지 않는다.

- 성공은 종료 코드 0, 실패는 1
- 문제 줄: `경로:줄: 무엇이 틀렸나 — 고칠 방법`
- 도구 오류(설정 없음·모르는 열쇠·git 실패·도구 없음·인자 오류)는 첫 줄이 `trust_checks: 오류: ` 로 시작한다
- 끝 줄 요약:
  - `comments: 바뀐 대상 파일 <N>개, 문제 없음` / `comments: 바뀐 대상 파일 <N>개, 문제 <M>개`
  - `expired: 만료가 지난 검사 끄기 표시 없음` / `expired: 만료가 지난 검사 끄기 표시 <N>개`
  - `repo: 문제 없음` / `repo: 문제 <N>개`
  - `metrics: 문제 없음` / `metrics: 문제 <N>개`
- `metrics` 는 지표마다 `<열쇠>=<현재> (기준선 <값>)` 한 줄, 이어서 `revert_ratio_6m=<비율> (되돌림 <a> / 머지 <b>)`(최근 6개월, 판정하지 않음), 그다음 `알림: …` 줄들
- `expired` 는 만료 목록이 있으면 1 로 끝난다. 읽지 못한 파일이 있으면 `trust_checks: 오류:` 로 시작해 목록 대신 오류를 낸다(밤 워크플로가 두 경우를 가른다)

## trust.toml 참조

모든 경로는 저장소 뿌리 기준 상대 경로다(절대 경로·`..` 는 실패, `[trust] node_modules` 만 절대 경로 허용. 저장소 안을 가리키는 절대 경로는 뿌리 기준 상대 경로로 바꿔 비교한다). 모르는 절·열쇠가 있으면 고칠 방법과 함께 실패한다. 절은 모두 생략할 수 있다(빈 파일이면 기본 기능만 돈다).

```toml
[trust]
baseline = "trust-baseline.json"
node_modules = "frontend/node_modules"

[comments]
exclude = []

[repo]
forbid_names = []
tool_settings_dirs = []

[metrics.ts_any]
roots = ["frontend/src"]

[metrics.knip]
dir = "frontend"

[metrics.ci_quarantine]
tests = "backend/tests"
marker = "ci_quarantine"

[metrics.prose_only_rules]
doc = "docs/standards.md"
heading = "규칙과 강제 수단"

[metrics.ty]
pyproject = "backend/pyproject.toml"
baseline = "backend/ty-baseline.json"

[metrics.import_linter]
pyproject = "backend/pyproject.toml"
contract = "<계약 이름>"

[metrics.banned_api]
pyproject = "backend/pyproject.toml"
code = "TID251"
allow = ["core/database.py", "tests/**"]

[metrics.eslint]
dir = "frontend"
exceptions = "frontend/eslint-exceptions.json"
contract = "frontend/eslint-contract.json"
```

### `[trust]`

| 열쇠 | 뜻 | 기본 |
|---|---|---|
| `baseline` | 기준 숫자 파일 | `trust-baseline.json` |
| `node_modules` | TS/JS 판정용 typescript 가 있는 `node_modules`. TS/JS 대상 파일이 있을 때만 필요 | 없음 |

### `[comments]`

- `exclude`: 주석 검사에서 뺄 글롭 목록(`**` 는 여러 폴더). 제외는 이 명시 목록뿐이다. 파일 머리말 표시로 스스로 빠지는 길은 없다. `node_modules`·`.venv`·lock 파일처럼 추적하는 생성물이 있으면 여기에 적는다.

### `[repo]`

- `forbid_names`: 추적하면 안 되는 파일 이름(예 `"CLAUDE.md"`)
- `tool_settings_dirs`: 이 폴더 아래(`"."` 은 뿌리)에서 `ruff.toml`·`.ruff.toml`·`ty.toml`·`.importlinter`·`setup.cfg`·`tox.ini`·`.flake8` 를 금지하고, `<폴더>/pyproject.toml` 이 아닌 `pyproject.toml` 에 `[tool.ruff]`·`[tool.ty]`·`[tool.importlinter]` 가 있으면 실패한다. 폴더가 겹치면 가장 깊은 폴더가 맡는다
- 항상 켜진 것: 이름에 `.disabled` 가 든 추적 파일 금지, `.ignore`·`.rgignore` 추적 금지, 무시 목록(`.gitignore` 등)에 걸린 대상 확장자 추적 파일 금지, `[trust] node_modules` 아래 파일 추적 금지

### 지표

기본 지표(항상 켜짐): `suppressions`, `allowed_comments_external`, `allowed_comments_license`, `allowed_comments_issue`, `disallowed_comments`(제외 밖 추적·새 대상 파일 전부, 여러 줄 주석·docstring 은 1개).

선택 지표는 절을 두면 켜진다. 모든 절에 `key = "…"` 를 두면 기준선 열쇠 이름을 덮어쓴다(기존 기준선과 이름을 맞출 때).

| 절 | 기본 열쇠 | 세는 것 | `--check` 가 더 막는 것 |
|---|---|---|---|
| `ts_any` | `ts_any` | `roots` 아래 TS/JS 의 `: any`·`as any`·`<any>` | |
| `knip` | `knip_unused` | `dir` 에서 `knip --reporter json` 의 이슈 배열 길이 합 | |
| `ci_quarantine` | `ci_quarantine` | `tests` 아래 `.py` 의 `marker` 표시 수(AST 로 셈) | |
| `prose_only_rules` | `prose_only_rules` | `doc` 의 `## <heading>` 절 표 `| 규칙 | 출처 | 강제 수단 | 상태 |` 에서 상태가 `문서뿐` 인 행(상태는 `강제됨`·`판단(사람)`·`문서뿐` 중 하나) | |
| `ty` | `ty_baseline_entries` | `baseline`(ty 기준선 JSON)의 진단 항목 수 | ty 기준선 삭제·항목 증가, `[tool.ty]` 의 `src.exclude` 추가·`src.include`·`baseline` 변경, 규칙 유효 수준 낮춤(전역·overrides, 기본 수준 표 기준) |
| `import_linter` | `import_linter_exceptions` | 이름이 `contract` 인 계약의 `ignore_imports` 수 | `[tool.importlinter]` 설정 변경, 계약 삭제·변경, 모든 계약의 새 `ignore_imports` 쌍(개수가 같아도 바꿔 끼우면 실패) |
| `banned_api` | `banned_api_exceptions` | ruff `per-file-ignores`(`extend-` 포함) 중 `code` 를 덮는 항목 수(`allow` 항목 제외) | `code` 가 ruff 우선순위상 꺼짐, 예외 글롭·없는 파일, 새 예외 열쇠, `banned-api` 이름 삭제, `extend` 변경, `exclude`·`extend-exclude` 추가 |
| `eslint` | `eslint_exceptions` | `exceptions` JSON(`{"<규칙>": ["src/…"]}`)의 배열 길이 합 | 새 (규칙, 경로) 쌍, 계약 파일 변경, 대상 파일마다 규칙이 `error` 인지·기준 파일과 옵션이 같은지, 검사용 원문이 기대만큼 막히는지 |

`code` 의 접두어는 앞 영문자 묶음부터 센다(`TID251` → `ALL`·`TID`·`TID2`·`TID25`·`TID251`). Pylint 계열(`PLC`·`PLE`·`PLR`·`PLW`)은 상위 `PL` 도 센다. 같은 수준이면 ignore 가, 다른 수준이면 더 구체적인 쪽이 이긴다(ruff 와 같다).

### eslint 계약 파일

`[metrics.eslint] contract` 가 가리키는 JSON 이다. 경로는 모두 `dir` 기준이다. 예는 `examples/aox3.eslint-contract.json`.

```json
{
  "files": ["src/**/*.ts", "src/**/*.tsx", "src/**/*.js", "src/**/*.jsx"],
  "reference": "src/index.tsx",
  "exempt": {"@typescript-eslint/no-restricted-imports": ["src/utils/auth.ts", "src/**/__tests__/**"]},
  "probe": {"path": "src/__trust_probe__/Probe.tsx", "lines": ["import axios from 'axios';"]},
  "expect": [{"label": "axios", "rule": "@typescript-eslint/no-restricted-imports", "line": 1, "count": 1}]
}
```

- `files`: 규칙 수준을 확인할 파일 글롭
- `reference`: 규칙 옵션의 기준 파일. 다른 파일의 옵션이 이것과 다르면 실패
- `exempt`: eslint 설정이 일부러 끈 파일 글롭(예외 파일에 적지 않는 영구 허용)
- `probe`: 검사용 원문. `path` 와(있으면) `reference` 위치에서 lint 한다
- `expect`: 원문의 `line` 에서 `rule` 이 최소 `count` 번 보고돼야 한다
- 확인할 규칙 = `expect` 의 규칙 ∪ 예외 파일의 규칙

## 주석 규칙 요약

- **대상:** `.py` `.pyi` `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs` `.rs` `.sh` `.bash` `.yml` `.yaml` `.toml`, 이름이 `Dockerfile` 로 시작하는 파일. 시험 코드도 대상
- **주석 판정:** 언어 구문을 아는 스캐너로 판정한다. Python 은 `#` 와 docstring(모듈·클래스·함수 첫 문장, 1행 shebang·1~2행 인코딩 선언 제외), TS/JS 는 TypeScript 스캐너(문자열·템플릿·정규식 안은 주석 아님, 맨 위 `/// <reference …/>` 지시문 제외), Rust 는 `//`·`/* */`(중첩), 셸은 따옴표·heredoc·산술 밖의 낱말 첫 `#`, YAML·TOML 은 그 언어의 주석(블록 스칼라 안은 문자열)
- **허용(1%):** 본문이 `외부: `(바깥 제약), `라이선스`, 이슈 주소(`https://…/issues/<숫자>`) 로 시작하거나 검사 끄기 표시
- **검사 끄기 표시:** `noqa`, `type: ignore`, `ty: ignore`, `pyright: ignore`, `pragma: no cover`, `fmt: off/on/skip`, `eslint-disable`(전 형태), `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck`, `prettier-ignore`, `istanbul ignore`, `c8 ignore`, `shellcheck disable`. 같은 줄에 `<이유> · 만료 YYYY-MM-DD`(eslint 는 `-- <이유> · 만료 …`)를 붙이거나, 바로 윗줄에 `검사 끄기: <이유> · 만료 YYYY-MM-DD` 주석을 둔다
- **바뀐 파일 규칙(`comments`):** 바뀐 대상 파일마다 ① 허용 안 된 주석이 파일 전체에서 0개 ② 모든 표시가 형식을 갖춤 ③ 만료 지난 표시 없음 ④ 새로 넣거나 바꾼 표시는 오늘부터 90일 이내 ⑤ 만료를 미룬 표시는 이유를 새로 적고 `baseline-approved` 표지가 있어야 통과
- 바뀌지 않은 파일의 기존 주석은 `disallowed_comments` 숫자로만 지켜본다

## 기준선 규칙(`metrics --check --base <ref>`)

- 현재 숫자를 **기준 ref 시점의 기준선**과 비교한다(작업 트리 기준선이 아니다). 늘면 실패, PR 이 기준선 값을 올려도 실패. `baseline-approved` 표지가 있으면 통과
- 줄면 알림만 낸다. 기준선을 낮추는 것은 선택
- 기준선 열쇠 집합 = 켜진 지표 집합. 다르면 실패. 기준 ref 기준선에 있던 지표를 끄거나 열쇠 이름을 바꾸면 표지가 필요하다. 새로 켠 지표는 작업 트리 기준선 값과 비교하고 알림을 낸다
- 기준선·ty 기준선 파일을 지우면 표지로도 통과하지 않는다. 지워진 뒤 다시 만든 기준선은 마지막으로 있던 기준선과 비교한다. 지워진 판은 `git log --full-history` 로 찾으므로 병합에서 지운 경우(한쪽 부모에만 있던 경우 포함)도 그 부모의 판을 되살린다. 이력에 있었는데 되살리지 못하면 표지로도 통과하지 않는다
- 기준 ref 에 기준선이 없으면(도입 첫 PR) 작업 트리 기준선과 비교하고 알림을 낸다
- `trust.toml` 완화도 막는다(표지 필요): `[comments] exclude` 추가, `[repo]` 목록에서 빼기, `[trust] baseline`·`[trust] node_modules` 경로 변경, 이미 있던 `[metrics.*]` 절의 설정 변경·삭제·열쇠 이름(`key`) 변경. 기준선 경로를 바꿔도 기준 ref 설정의 기준선과 비교한다
- 비교할 기준 설정은 `--config` 와 따로 **기준 ref 의 `<root>/trust.toml`** 이다. 기준 ref 에 없으면 이력(`--full-history`, 병합 부모 포함)에서 지워지기 직전 판을 쓴다. `--config` 로 저장소 밖이나 다른 경로의 설정을 골라도 이 기준 설정과 비교한다. 이력에 `trust.toml` 이 한 번도 없을 때만 첫 도입으로 보고 알림을 낸다. 이력에 있었는데 되살리거나 읽지 못하면 실패한다(표지 필요). 얕은 클론이라 이력에 없다고 확인할 수 없을 때도 실패한다(CI 는 `fetch-depth: 0`)
- 켜진 선택 절의 원천이 없으면 표지로도 통과하지 않는다: 설정에 적은 파일(`[metrics.ty] pyproject` 등)이 없거나 읽히지 않을 때, 폴더(`roots`·`dir`·`tests`)가 없을 때. 다른 절이 같은 파일을 대신 확인한다고 보지 않는다(`metrics` 도 같은 오류로 실패한다)

## 워크플로 틀

`templates/` 를 저장소의 `.github/workflows/` 로 복사하고 자리 표시를 채운다.

| 파일 | 하는 일 |
|---|---|
| `templates/trust-gate.yml` | PR 마다(표지를 달거나 떼도) `comments --base`·`metrics --check --base`·`repo` |
| `templates/trust-expiry.yml` | 매일 06:00(한국 시각) `expired`. 있으면 열린 이슈 `만료된 검사 끄기 표시`(표지 `trust-expiry`)를 만들거나 본문을 바꾸고, 없으면 닫는다. 표지 `baseline-approved`·`trust-expiry` 가 없으면 만든다 |

| 자리 표시 | 채울 것 |
|---|---|
| `__RUNNER_LABELS__` | 러너 라벨(예 `[self-hosted, macOS]`) |
| `__INSTALL_TS_DEPENDENCIES__` | TS 의존 설치(예 `cd frontend && bun install --frozen-lockfile`). TS/JS 대상이 없으면 이 단계와 `setup-bun` 을 지운다 |

공개 저장소에서 self-hosted 러너를 쓰면 잡에 `if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository` 조건을 넣는다(바깥 fork PR 이 러너에서 코드를 돌리지 못하게).

`baseline-approved` 표지는 사람만 단다. 에이전트는 달지 않는다.

## 판 올리기 절차

1. 이 저장소에서 바꾸고 시험을 통과시킨다(아래 「개발」).
2. 태그를 붙이기 전에 `pyproject.toml` 의 `version` 을 태그와 같게 올린다(v0.1.1 태그는 이 단계를 빠뜨려 패키지 판이 0.1.0 으로 남았다). 그런 다음 태그 `v<판>` 을 단다. 이미 단 태그는 옮기지 않는다.
3. 쓰는 저장소는 워크플로·훅의 `@v<판>` 을 새 태그로 바꾸는 PR 을 낸다. 지표 셈법이 바뀌었으면 같은 PR 에서 `metrics --update` 로 기준선을 다시 쓴다(늘면 표지 필요).

### ty 판을 올리면 기본 수준 표를 다시 만든다

`src/trust_checks/ty_rule_defaults.json` 은 ty 규칙의 기본 수준 표다(설정에 수준을 적지 않은 규칙을 낮췄는지 판정할 때 쓴다). 쓰는 저장소의 ty 판을 올리면 같은 판으로 표를 다시 만든다.

```sh
ty --version
ty explain rule > "$TMPDIR/ty-rules.txt"
.venv/bin/python -c 'import sys; from trust_checks import settings; sys.stdout.write(settings.render_ty_defaults(sys.argv[1], open(sys.argv[2], encoding="utf-8").read()))' <판> "$TMPDIR/ty-rules.txt" > src/trust_checks/ty_rule_defaults.json
.venv/bin/python -m pytest -q -k TyDefaults
```

`-k TyDefaults` 는 `TestRereviewTyDefaults` 의 시험을 고른다. 그중 `test_defaults_table_matches_real_ty` 는 표의 판과 같은 ty 가 있으면 실제 `ty explain rule` 과 표가 같은지 대조한다.

## 개발

```sh
bun install --frozen-lockfile
uv sync --locked
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/ty check --python .venv
.venv/bin/trust-checks comments --base origin/main
.venv/bin/trust-checks repo
.venv/bin/trust-checks metrics --check --base origin/main
```

- 시험은 임시 git 저장소와 `trust.toml` 으로 돈다. TS·eslint 시험은 이 저장소의 `node_modules` 를 쓴다
- 이 저장소의 코드·설정·시험·워크플로도 주석 금지를 지킨다(뿌리의 `trust.toml` 로 자기 자신을 검사한다)
- `examples/aox3.trust.toml` 은 aox3 저장소용 설정 예다. `TRUST_CHECKS_AOX3=<aox3 체크아웃>` 을 주면 그 저장소로 예시 설정을 대조하는 시험이 돈다
