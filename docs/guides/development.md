# 개발 가이드

> 명령, DB 마이그레이션, 도구와 에이전트 추가, 테스트, 이미지 빌드 메모.
> 관련 코드: `Makefile`, `backend/migrations/`, `backend/app/core/db.py`, `backend/app/tools/registry.py`, `backend/app/agents/__init__.py`, `backend/app/orchestration/tooling.py`, `backend/scripts/prefetch_ocr_models.py`, `docker/postgres/Dockerfile`

실행 환경과 `.env` 설정은 [실행 환경 설정](setup.md)에 있습니다.

## make 명령

`make` 나 `make help` 를 입력하면 전체 목록이 나옵니다. `Makefile` 의 목표는 아래가 전부입니다.

| 명령 | 설명 |
|---|---|
| `make up` | 전체 실행. 바뀐 이미지는 다시 빌드한다 |
| `make down` | 전체 중지. 데이터는 남는다 |
| `make ps` | 컨테이너 상태 |
| `make logs` | 백엔드 로그 (Ctrl+C 로 끝낸다) |
| `make backend` | 백엔드만 다시 빌드·재시작 (코드·프롬프트를 고친 뒤). Langfuse 미디어 전달기가 있으면 다시 붙인다 |
| `make frontend` | 프론트엔드만 다시 빌드·재시작 |
| `make prompt-sync` | 프롬프트를 Langfuse 로 동기화. 바뀐 것만 새 버전이 된다 |
| `make prompt-check` | 동기화하면 무엇이 바뀌는지만 본다 (`--dry-run`) |
| `make db-revision m="..."` | 모델 변경을 비교해 마이그레이션 파일을 만든다 |
| `make db-upgrade` | 최신 마이그레이션을 적용한다 |
| `make db-downgrade` | 마이그레이션을 한 단계 되돌린다 |
| `make db-check` | 모델과 마이그레이션이 어긋났는지 검사한다. 어긋나면 실패한다 |
| `make db-status` | 현재 리비전과 이력 |
| `make test` | 백엔드 테스트. LLM 없이 돈다 |
| `make lint` | 백엔드 `ruff check`, `ruff format --check`, 계층 검사(`lint-imports`), 프론트엔드 빌드 |
| `make docs-check` | 문서 검사: mermaid 문법, 상대 링크와 앵커, ERD 가 `models.py` 와 같은지 |
| `make docs-erd` | `models.py` 로 `docs/data/erd.md` 의 ERD 를 다시 만든다 |

`make` 는 현재 커밋 해시를 `GIT_COMMIT` 으로 모든 `docker compose` 명령에 넘깁니다. 커밋하지 않은 변경이 있으면 `-dirty` 가 붙습니다.
Langfuse 의 프롬프트 버전에 어떤 코드에서 나왔는지가 이 값으로 남습니다. `docker compose` 를 직접 쓰면 값이 비므로 `make` 를 쓰세요.

명령마다 실행되는 곳이 다릅니다.

- `prompt-sync`, `prompt-check` 는 실행 중인 `backend` 컨테이너 안에서 돕니다(`docker compose exec`). 먼저 `make up` 을 해 두어야 합니다.
- `db-*` 는 호스트에서 `uv run alembic` 으로 돕니다. `.env` 의 `POSTGRES_*` 값으로 `127.0.0.1` 의 Postgres 에 붙으므로 `postgres` 컨테이너가 떠 있어야 합니다.
- `test`, `lint` 는 호스트에서 `uv` 와 `npm` 으로 돕니다.

## DB 마이그레이션 (Alembic)

Alembic 은 `backend/app/models.py` 에 정의한 앱 테이블 10개를 모두 관리합니다.
LangGraph 의 checkpointer 와 Store 테이블은 LangGraph 가 `setup()` 에서 직접 만듭니다. `migrations/env.py` 의 `include_name` 이
`app.models` 에 없는 테이블을 비교 대상에서 빼서, autogenerate 가 그 테이블을 지우자고 제안하지 않습니다.
테이블별 저장 내용은 [데이터 저장 위치](../data/storage-map.md), 관계는 [ERD](../data/erd.md)에 있습니다.

백엔드는 시작할 때 최신 마이그레이션을 자동으로 적용합니다(`app.core.db.run_migrations`). 평소에는 따로 할 일이 없습니다.

모델을 바꿨을 때는 아래 순서를 따릅니다.

```bash
make db-revision m="add tags to conversations"   # backend/migrations/versions/ 에 파일이 생긴다
# 생긴 파일을 읽고 고칠 곳을 고친다
make db-check                                     # 모델과 마이그레이션이 맞는지 확인
make backend                                      # 재시작하면 자동 적용 (또는 make db-upgrade)
```

- autogenerate 결과는 그대로 믿지 말고 읽어 봅니다. 데이터 옮기기, 서버 기본값, 확장(`vector` 등)은 직접 써야 할 때가 있습니다.
- Alembic 을 들이기 전에 만든 DB 는 `alembic_version` 없이 `conversations` 테이블만 있습니다. 이런 DB 는 첫 기동 때 기준 리비전 `0001` 로 표시만 하고 그 뒤 마이그레이션을 적용합니다.
- 앱 시작 때의 자동 적용은 uvicorn 워커가 하나라고 가정합니다. 여러 프로세스가 동시에 적용하면 충돌할 수 있습니다.

## 도구 추가하기

도구는 `app/tools/<주제>.py` 에 만들고 `app/tools/registry.py` 의 `TOOLS` 에 등록합니다. 그다음 쓸 에이전트의 `TOOL_NAMES` 에 이름을 넣습니다.

```python
# app/tools/docs.py
@tool(parse_docstring=True)
def search_docs(query: str) -> str:
    """Search internal documents.

    Args:
        query: Search keywords.
    """
    ...

# app/tools/registry.py
TOOLS["search_docs"] = ToolSpec(
    lambda settings: search_docs,                          # build: 설정을 받아 도구를 만든다
    ToolLabel(running="문서를 검색하는 중", done="문서 검색함"),  # 화면의 실행 상태 문구
    "documents",                                           # 그룹 (GROUPS 의 이름)
)

# app/agents/chat/graph.py
TOOL_NAMES = ("get_current_time", ..., "search_docs")
```

- **docstring 은 영어로 씁니다.** 모델에게 도구 설명으로 전달되기 때문입니다. 언제 쓰고 언제 쓰지 말지도 적습니다.
  `parse_docstring=True` 로 선언하면 `Args` 설명이 도구 인자 스키마에 들어갑니다.
- **`ToolSpec.build`** 는 설정을 받아 도구를 돌려줍니다. 지금 설정으로 쓸 수 없으면(API 키 없음, 기능 꺼짐) `None` 을 돌려주고, 그러면 그 도구는 빠집니다.
  예를 들어 `web_search` 는 `TAVILY_API_KEY` 가 없으면, 기억 도구는 `ENABLE_MEMORY=false` 면 빠집니다.
- **그룹.** 도구마다 그룹(`time`, `web`, `documents`, `memory`)을 정합니다. 도구 결정을 켜면 결정 모델이 이 그룹 단위로 도구를 고릅니다.
  새 그룹을 만들면 `GROUPS` 에 `ToolGroup(이름, 질문)` 을 더합니다. 질문은 결정 모델에게 묻는 예/아니오 질문이고 영어로 씁니다.
  결정 방식은 [도구 결정](../features/tool-decision.md)에 있습니다.
- **오류.** 도구 안에서 예외가 나도 응답 전체가 실패하지 않습니다. `ToolNode` 가 `tooling.handle_tool_error` 로 예외를 오류 결과로 바꾸고, 화면에는 그 단계가 "실패" 로 보입니다.
  모델에게 알려 줄 문구가 있으면 `app.core.errors.ToolError("짧은 영어 문구")` 를 던집니다. 그 밖의 예외는 원문을 로그에만 남기고 모델에게는 일반 문구를 줍니다.
  내부 주소, 키, 예외 원문은 `ToolError` 문구에 넣지 않습니다.
- **반복 제한.** 한 턴에 그래프 단계는 16번까지입니다(`tooling.RECURSION_LIMIT`). 도구를 한 번 쓰면 agent 와 tools 두 단계가 돌므로 도구 호출은 약 7번까지입니다.
  넘으면 응답을 멈추고 "도구를 너무 여러 번 호출해서 응답을 중단했습니다" 안내를 보여 줍니다.
- 외부 웹에 붙는 도구는 [웹 검색과 페이지 읽기](../features/web-fetch.md)의 SSRF 방어와 인젝션 방어를 따릅니다.

## 에이전트 추가하기

1. `app/agents/<이름>/graph.py` 에 `build_graph`, `STREAM_NODES`, `TOOL_NAMES` 를 만듭니다. `build_graph` 는 `(settings, checkpointer, store)` 를 받아 컴파일된 그래프를 돌려줍니다.
2. `app/agents/<이름>/__init__.py` 에서 `TOOL_LABELS = tool_labels(TOOL_NAMES)` 를 만들어 내보냅니다(`app/agents/chat/__init__.py` 참고).
3. `app/agents/__init__.py` 의 `AGENTS` 에 `AgentSpec(build_graph, STREAM_NODES, TOOL_LABELS)` 로 등록합니다. 앱이 시작할 때 실행 엔진(`app/orchestration/runtime.py`)에 등록됩니다.
4. 프롬프트는 `app/agents/<이름>/prompts/*.md` 에 둡니다. 폴더 이름이 프롬프트 네임스페이스(`<이름>/...`)가 됩니다.

`STREAM_NODES` 에 넣은 노드가 만든 토큰만 화면으로 스트리밍됩니다. 요약 노드처럼 사용자에게 보이면 안 되는 노드는 빼 둡니다.

## 프롬프트

프롬프트는 `app/agents/<에이전트>/prompts/*.md` 와 공용 조각 `app/prompts/shared/` 에 있고, 코드에서는 `"chat/system"` 같은 이름으로 부릅니다.
그래프 노드 같은 비동기 코드에서는 `await prompts.arender(...)` 를 씁니다. `render` 는 Langfuse 조회 때문에 이벤트 루프를 막을 수 있습니다.

```bash
make prompt-check   # 동기화하면 바뀔 것만 본다
make prompt-sync    # 바뀐 프롬프트만 Langfuse 에 새 버전으로 올린다
```

파일 형식, 검증 규칙, 라벨과 롤백은 [프롬프트](../features/prompts.md)에 있습니다.

## 테스트와 린트

```bash
make test   # cd backend && uv run pytest -q
make lint   # ruff, import 계층 검사, 프론트엔드 빌드
```

- 테스트는 실제 LLM 을 부르지 않습니다. fake 모델, `InMemoryStore` 같은 대역으로 프롬프트, 그래프, 대화 이력, 컨텍스트 관리, 도구, 승인 흐름 등을 검사합니다.
- 파일 하나만 돌리려면 `cd backend && uv run pytest -q tests/test_tools.py` 처럼 씁니다.
- `make lint` 의 계층 검사는 `backend/pyproject.toml` 의 `[tool.importlinter]` 규칙을 씁니다. 규칙은 [계층 규칙](../architecture/layers.md)에 있습니다.
- DB 스키마를 바꿨으면 `make db-check` 도 돌립니다.

## 문서 검사

`docs/` 나 `README.md` 를 고쳤으면 `make docs-check` 를 돌립니다. 세 가지를 검사합니다.

| 검사 | 도구 | 잡는 것 |
|---|---|---|
| mermaid 문법 | `docs/tools/check_mermaid.mjs` (mermaid 11.4.1 을 jsdom 위에서 parse) | GitHub 에서 오류 상자로 보일 그림. 예약어 노드 ID(`end`, `graph`, `call`) 같은 실수 |
| 링크 | `docs/tools/check_links.py` | 없는 파일, 없는 `#앵커` (GitHub 방식으로 앵커를 만듦) |
| ERD | `backend/scripts/gen_erd.py --check` | `docs/data/erd.md` 의 ERD 가 `models.py` 와 다름 |

- 처음 돌릴 때 `docs/tools/` 에 `npm ci` 로 검사 도구를 설치합니다. 버전은 `package-lock.json` 에 고정되어 있습니다.
- 모델을 바꿨으면 `make docs-erd` 로 ERD 를 다시 만듭니다. `erd.md` 의 `<!-- erd:영역 -->` 사이만 바뀌고 설명 글은 그대로입니다.
- 새 테이블을 만들면 `gen_erd.py` 의 `GROUPS` 에 영역을 정해야 합니다. 정하지 않으면 검사가 실패합니다.

## 이미지 빌드 메모

### OCR 모델 미리 받기

문서 OCR 은 PaddleOCR 모델을 씁니다. 모델은 처음 OCR 할 때 Hugging Face 에서 받는데, 이미지에 미리 넣어 두면 첫 업로드가 느려지거나 네트워크가 없어 실패하지 않습니다.

- `backend/Dockerfile` 이 `backend/scripts/prefetch_ocr_models.py` 를 실행합니다. 이 스크립트는 빈 PNG 를 한 번 OCR 해서 모델(약 30MB)을 받습니다.
- 앱 코드보다 먼저 복사·실행하므로 앱 코드가 바뀌어도 모델을 다시 받지 않습니다.
- 그래서 스크립트는 앱 모듈을 불러오지 않고 언어 값 `OCR_LANGUAGE = "korean"` 을 따로 둡니다. `app.documents.parser.OCR_LANGUAGE` 를 바꾸면 스크립트도 같이 바꿔야 합니다. 두 값이 다르면 `tests/test_documents.py` 가 실패합니다.

### Postgres 이미지

앱 Postgres 는 `docker/postgres/Dockerfile` 로 만든 이미지를 씁니다. `postgres:17-alpine` 에 pgvector(`v0.8.7`)를 소스에서 빌드해 넣은 것입니다.

공식 pgvector 이미지(`pgvector/pgvector`)를 쓰지 않는 이유는 C 라이브러리입니다. 공식 이미지는 Debian(glibc) 기반입니다.
이전에 alpine(musl) 이미지로 만든 데이터 디렉터리를 그대로 붙이면 문자열 정렬 규칙(collation)이 달라 텍스트 인덱스가 어긋날 수 있습니다.
기존 alpine 이미지에 pgvector 만 더하면 데이터를 옮기지 않아도 됩니다.

빌드는 `with_llvm=no`(JIT 비트코드 없음)와 `OPTFLAGS=""`(`-march=native` 빼기)로 합니다. 그래서 clang 없이 빌드되고, 빌드한 기계와 다른 CPU 에서도 돕니다.
