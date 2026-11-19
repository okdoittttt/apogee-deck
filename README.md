# Agent

LangGraph 기반 LLM 에이전트 웹앱. Docker Compose로 실행합니다.

```
브라우저 ──▶ frontend (Next.js)
   │
   └── POST /api/chat (SSE) ──▶ backend (FastAPI)
                                   │
                                   ├─ LangGraph 에이전트 ──▶ LLM (Anthropic | Ollama | fake)
                                   │      └─ checkpointer ──▶ postgres (에이전트 상태)
                                   ├─ messages 테이블 ─────▶ postgres (UI 표시용 대화 이력)
                                   └─ 트레이스 ────────────▶ Langfuse (셀프호스팅)
```

## 실행

```bash
cp .env.example .env        # LLM_PROVIDER, ANTHROPIC_API_KEY 등 설정
docker compose up -d --build
```

| 서비스 | 주소 |
|---|---|
| 채팅 UI | http://localhost:3000 |
| 백엔드 API | http://localhost:8000/docs |
| Langfuse | http://localhost:3100 (`.env`의 `LANGFUSE_INIT_USER_EMAIL` / `PASSWORD`로 로그인) |

포트가 이미 사용 중이면 `.env`의 `*_PORT` 값을 바꾸세요.
Langfuse 없이 앱만 띄우려면 `.env`의 `COMPOSE_FILE` 줄을 주석 처리하고 `LANGFUSE_*_KEY`를 비우세요.

## LLM provider 전환

`.env`의 `LLM_PROVIDER`만 바꾸고 `docker compose up -d backend`를 실행하면 됩니다.

| 값 | 설명 |
|---|---|
| `anthropic` | Claude (`ANTHROPIC_API_KEY` 필요) |
| `ollama` | 로컬 모델. **macOS에서는 Ollama를 호스트에서 직접 실행**하세요(Docker 안에서는 Metal GPU를 못 씀) |
| `fake` | LLM 없이 받은 메시지를 그대로 돌려줌. 파이프라인과 트레이싱 검증용 |

Ollama 예시:
```bash
brew install ollama && ollama serve
ollama pull qwen3.5:9b     # .env: LLM_PROVIDER=ollama, OLLAMA_MODEL=qwen3.5:9b, OLLAMA_REASONING=true
ollama show qwen3.5:9b     # capabilities(tools/thinking/vision), 최대 context 확인
ollama ps                  # 실제 로드된 context 크기, 메모리, runner 확인
```
tool calling을 지원하지 않는 모델이면 `ENABLE_TOOLS=false`로 설정하세요.

### 이미지 입력 (VLM)
채팅 입력창에서 이미지 버튼, 붙여넣기, 드래그 앤 드롭으로 첨부합니다(메시지당 최대 4장).
- 프론트엔드가 긴 변 1024px로 줄여서 base64로 전송 → 백엔드가 검증(형식, 5MB, 장수) 후 `message_images` 테이블에 저장
- LangChain 표준 이미지 블록(`{"type": "image", "base64", "mime_type"}`)으로 만들어 그래프에 전달 → Anthropic / Ollama 형식으로 자동 변환
- 화면에서는 `GET /api/images/{id}`로 이미지를 불러옴
- vision을 지원하지 않는 모델이면 `ENABLE_VISION=false` (이미지 첨부 시 400 응답)

### 컨텍스트 관리
| 설정 | 의미 |
|---|---|
| `OLLAMA_NUM_CTX` | 모델이 한 번에 보는 토큰 수(입력+출력). 클수록 KV 캐시 메모리 증가 |
| `OLLAMA_NUM_PREDICT` | 출력 최대 토큰 (thinking 포함) |
| `CONTEXT_MAX_TOKENS` | 모델에 보낼 대화 이력 예산. 기본값 `NUM_CTX - NUM_PREDICT - 256` |
| `IMAGE_TOKEN_ESTIMATE` | 예산 계산 시 이미지 1장의 토큰 수 (기본 1500) |

Ollama는 `num_ctx`를 넘는 입력을 **오류 없이 앞부분부터 잘라냅니다**. 그래서 `agent` 노드가 호출 전에
`trim_messages`로 최근 대화만 예산 안에 맞춥니다. checkpointer에 저장된 원본 이력은 그대로 남고,
모델에 실제로 들어간 입력은 Langfuse에서 확인할 수 있습니다. (Anthropic은 컨텍스트가 크고 이력을
자르면 캐시가 깨지므로 기본적으로 자르지 않습니다.)

## 구조

```
backend/
├── app/
│   ├── main.py              # 앱, CORS, lifespan (DB 초기화 + 에이전트 런타임 시작/종료)
│   ├── config.py            # 환경 변수 기반 설정
│   ├── db.py / models.py    # SQLAlchemy (UI용 conversations, messages, message_images)
│   ├── schemas.py
│   ├── tracing.py           # Langfuse CallbackHandler
│   ├── routers/chat.py      # /api/chat (SSE), /api/conversations, /api/images
│   ├── llm/                 # provider 별 chat model 생성 (모든 에이전트 공용) + fake 모델
│   ├── prompts/             # 프롬프트 로더 + 공용 조각
│   │   ├── loader.py
│   │   └── shared/          # language.md, formatting.md ...
│   └── agents/
│       ├── runtime.py       # 에이전트 레지스트리, checkpointer, 스트리밍 실행
│       ├── context.py       # 공용: 대화 이력 trim, 프롬프트 동적 변수(오늘 날짜)
│       └── chat/            # 기본 채팅 에이전트
│           ├── graph.py     # LangGraph: agent ⇄ tools
│           ├── tools.py
│           └── prompts/system.md
└── tests/                   # uv run pytest
```

**대화 이력 저장 방식 (A안)**
- LLM에 들어가는 이력은 LangGraph checkpointer가 `thread_id = conversation_id` 단위로 관리합니다. 요청마다 새 사용자 메시지만 그래프에 넣습니다.
- `messages` 테이블은 UI에 대화를 보여주는 용도로만 씁니다.

## 프롬프트 관리

프롬프트는 **각 에이전트 폴더**(`app/agents/<에이전트>/prompts/`)에 두고, 여러 에이전트가 같이 쓰는 규칙은
**공용 조각**(`app/prompts/shared/`)으로 분리해 `include` 합니다. 코드에서는 경로가 아니라 이름으로 부릅니다.

```python
from app import prompts
prompts.render("chat/system", today="2026-11-19 (Thursday)").text
```

| 이름 | 파일 |
|---|---|
| `chat/system` | `app/agents/chat/prompts/system.md` |
| `shared/language` | `app/prompts/shared/language.md` |

파일 형식은 YAML frontmatter + Jinja2 본문입니다.

```markdown
---
description: 기본 채팅 에이전트의 system 프롬프트
variables: [today]            # 렌더링에 필요한 변수
---
Today's date is {{ today }}.

{% include "shared/language.md" %}
```

검증 규칙 (앱 시작 시 + `uv run pytest`):
- `variables`에 선언한 변수와 템플릿(include 포함)에서 실제로 쓰는 변수가 정확히 일치해야 합니다.
- 렌더링할 때 변수가 빠지거나, 선언하지 않은 변수를 넘기면 오류가 납니다.
- include 대상 파일이 없으면 오류가 납니다.

**잘못된 프롬프트는 배포 후 첫 요청이 아니라 앱 기동 시점에 실패합니다.**

### Langfuse 프롬프트 관리 연동

git(로컬 파일)이 **원본**이고, Langfuse에는 버전별로 동기화됩니다. 각 LLM 호출 트레이스에는
"어떤 프롬프트의 몇 번째 버전을 썼는지"가 연결되어, Langfuse 화면에서 버전별 지연/토큰/호출 수를 비교할 수 있습니다.

```
system.md 수정 ─▶ sync ─▶ Langfuse: chat/system v1 · v2 · v3 (production 라벨)
                                         │
백엔드 실행 중: production 라벨 버전 사용 ◀┘  (Langfuse 연결 실패 시 로컬 파일)
트레이스: ChatOllama generation ↔ chat/system v3
```

| 설정 | 의미 |
|---|---|
| `PROMPT_SOURCE` | `langfuse`: 라벨이 붙은 버전 사용, 실패 시 로컬 파일 / `file`: 항상 로컬 파일 |
| `PROMPT_LABEL` | 실행 시 가져올 라벨 = 동기화 시 새 버전에 붙일 라벨 (기본 `production`) |
| `PROMPT_SYNC_ON_STARTUP` | 백엔드 시작 시 자동 동기화 (개발 환경용) |

```bash
docker compose exec backend python -m app.prompts.sync            # 동기화
docker compose exec backend python -m app.prompts.sync --dry-run  # 변경 사항만 확인
```

- **include는 합쳐서, 변수는 `{{var}}` 형식으로** 올라갑니다. 그래서 Jinja 문법은 변수 치환과 정적 include만 허용합니다(검증에서 확인).
- **내용이 바뀐 경우에만 새 버전**이 생깁니다. 공용 조각을 고치면 그 조각을 쓰는 모든 프롬프트의 새 버전이 생깁니다.
- **롤백:** Langfuse 화면에서 `production` 라벨을 이전 버전으로 옮기면 재배포 없이 바로 반영됩니다(캐시 60초).
  이후 sync를 실행해도 최신 버전과 내용이 같으면 아무것도 바꾸지 않으므로 롤백 상태가 유지됩니다.
- Langfuse 화면에서 프롬프트를 직접 고치는 것은 실험용으로만 쓰세요. 변수 구성이 코드와 다르면 그 버전은 쓰지 않고 로컬 파일을 사용합니다.

## 에이전트 / 도구 추가하기

**도구:** `app/agents/chat/tools.py`에 함수를 추가하고 `TOOLS`에 넣으면 그래프에 자동으로 연결됩니다.

```python
@tool
def search_docs(query: str) -> str:
    """Search internal documents."""
    ...

TOOLS = [get_current_time, search_docs]
```

**에이전트:**
1. `app/agents/<이름>/`에 `graph.py`(`build_graph`, `STREAM_NODES`)와 `prompts/*.md`를 만듭니다.
2. `app/agents/runtime.py`의 `AGENTS`에 등록합니다.
3. 프롬프트 네임스페이스(`<이름>/...`)는 폴더 이름으로 자동 인식됩니다.

## 테스트

```bash
cd backend && uv run pytest     # LLM 없이(fake 모델) 프롬프트, 그래프, 대화 기억, 잘라내기 검증
```

## API

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/chat` | `{conversation_id?, message, images?: [{mime_type, data}]}` → SSE 이벤트 `meta` / `delta` / `done` / `error` |
| GET | `/api/conversations` | 대화 목록 |
| GET | `/api/conversations/{id}` | 대화 + 메시지 |
| DELETE | `/api/conversations/{id}` | 대화 삭제 |
| GET | `/api/images/{id}` | 첨부 이미지 원본 |
| GET | `/health` | 헬스 체크 |

## 다음 단계

- Alembic 마이그레이션 (현재는 시작 시 `create_all`로 테이블 생성)
- 장기 기억 메모리 (LangGraph Store 등)
- Langfuse 데이터셋 + 평가(LLM-as-judge)로 프롬프트 버전 간 품질 비교
- CI에서 main 머지 시 프롬프트 자동 동기화
- 대화 삭제 시 checkpointer 데이터도 함께 정리
- 이미지를 object storage(MinIO 등)에 두고 상태에는 참조만 저장 (지금은 checkpoint 에 base64 가 반복 저장됨)
- 인증, rate limiting
- Langfuse 활용: 프롬프트 관리, 평가(datasets / LLM-as-judge), 비용 대시보드
