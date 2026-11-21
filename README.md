# okdoitttt

LangGraph 기반 LLM 에이전트 웹앱. Docker Compose로 실행합니다.

```
브라우저 ──▶ frontend (Next.js)
   │
   └── POST /api/chat (SSE) ──▶ backend (FastAPI)
                                   │
                                   ├─ LangGraph 에이전트 ──▶ LLM (Anthropic | OpenAI | Google | Ollama | fake)
                                   │      └─ checkpointer ──▶ postgres (에이전트 상태)
                                   ├─ messages 테이블 ─────▶ postgres (UI 표시용 대화 이력)
                                   ├─ 첨부 원본 ───────────▶ minio (앱 전용, S3 API)
                                   └─ 트레이스 ────────────▶ Langfuse (셀프호스팅)
```

## 실행

```bash
cp .env.example .env        # LLM_PROVIDER, ANTHROPIC_API_KEY 등 설정
make up                     # 전체 실행 (변경된 이미지는 다시 빌드)
```

자주 쓰는 명령은 `Makefile`에 모아 두었습니다. `make`만 입력하면 목록이 나옵니다.

| 명령 | 설명 |
|---|---|
| `make up` / `make down` | 전체 실행 / 중지 (데이터 유지) |
| `make backend` / `make frontend` | 해당 서비스만 다시 빌드·재시작 (코드·프롬프트 수정 후) |
| `make ps` / `make logs` | 컨테이너 상태 / 백엔드 로그 |
| `make prompt-sync` / `make prompt-check` | 프롬프트 Langfuse 동기화 / 변경 사항만 확인 |
| `make test` / `make lint` | 백엔드 테스트 / 린트·빌드 검사 |

`make`는 현재 커밋 해시를 `GIT_COMMIT`으로 자동 전달합니다(커밋 안 된 변경이 있으면 `-dirty`).
그래서 Langfuse 프롬프트 버전에 어떤 코드에서 나온 버전인지 기록됩니다. `docker compose`를 직접 쓰면 이 값이 비게 되므로 `make`를 쓰세요.

| 서비스 | 주소 |
|---|---|
| 채팅 UI | http://localhost:3000 |
| 백엔드 API | http://localhost:8000/docs |
| Langfuse | http://localhost:3100 (`.env`의 `LANGFUSE_INIT_USER_EMAIL` / `PASSWORD`로 로그인) |
| MinIO 콘솔 (첨부 저장소) | http://localhost:9101 (`.env`의 `MINIO_ROOT_USER` / `PASSWORD`로 로그인) |

포트가 이미 사용 중이면 `.env`의 `*_PORT` 값을 바꾸세요.
모든 포트는 기본적으로 `127.0.0.1`에만 열려서 이 컴퓨터에서만 접속할 수 있습니다.
이 앱에는 인증이 없으므로, 다른 기기에서 접속하도록 `BIND_HOST=0.0.0.0`으로 바꿀 때는 `.env.example`의 설명을 먼저 읽으세요.
Langfuse 없이 앱만 띄우려면 `.env`의 `COMPOSE_FILE` 줄을 주석 처리하고 `LANGFUSE_*_KEY`를 비우세요.

## 모델 선택

입력창 오른쪽 아래의 드롭다운에서 모델을 고릅니다. 고른 모델은 다음에 보내는 메시지부터 쓰이고,
브라우저에 기억됩니다. 대화 도중에 바꿔도 이력은 이어지며, 각 답변 아래에 응답한 모델이 표시됩니다.

목록에는 **지금 쓸 수 있는 모델만** 나옵니다(`GET /api/models`, `app/llm/catalog.py`).

| provider | 목록에 나오는 조건 | 모델 |
|---|---|---|
| `openai` | `OPENAI_API_KEY` 가 있을 때 | `OPENAI_MODELS` (기본 gpt-6-luna, gpt-5.6-luna, gpt-6.1-terra) |
| `google` | `GOOGLE_API_KEY` 가 있을 때 | `GOOGLE_MODELS` (기본 Gemma 4 26B, 31B) |
| `anthropic` | `ANTHROPIC_API_KEY` 가 있을 때 | `ANTHROPIC_MODELS` (기본 Opus, Sonnet, Haiku) |
| `ollama` | Ollama 서버가 응답할 때 | 설치된 모델 전체 (`ollama list`). 꺼져 있으면 빠집니다 |
| `fake` | `LLM_PROVIDER=fake` 일 때 | 테스트용 |

- `.env`의 `LLM_PROVIDER`와 `*_MODEL`은 **처음 선택될 기본 모델**입니다. 그 모델을 쓸 수 없으면 목록의 첫 모델이 선택됩니다.
- 모델 ID는 `"<provider>:<모델 이름>"` 형식입니다(예: `google:gemma-4-26b-a4b-it`, `ollama:qwen3.5:9b`).
- 모델마다 provider와 컨텍스트 예산이 달라서 그래프를 모델별로 만들어 캐시합니다(`runtime.get_graph`). 대화 상태는 같은 checkpointer에 있습니다.
- 대화 도중 모델을 바꾸면 이전 답변의 추론 블록은 새 모델에 보내지 않습니다(`orchestration/context.py`의 `drop_foreign_reasoning`).
  추론 블록에는 만든 provider의 서명이 붙어 있어서, 다른 provider에 보내면 요청이 거절될 수 있기 때문입니다. 답변 텍스트와 도구 호출은 그대로 보냅니다.

### provider

| 값 | 설명 |
|---|---|
| `anthropic` | Claude (`ANTHROPIC_API_KEY` 필요) |
| `openai` | OpenAI Responses API (`OPENAI_API_KEY` 필요). 추론 요약을 "생각 과정"에 보여줌 |
| `google` | Gemini API (`GOOGLE_API_KEY` 필요). 기본 모델은 무료 티어로 쓸 수 있는 `gemma-4-26b-a4b-it` |
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

OpenAI:
- 선불 크레딧에서 차감되므로, 드롭다운에는 `OPENAI_MODELS`에 적은 모델만 나옵니다(비싼 모델이 실수로 쓰이지 않게).
- 추론 강도는 `OPENAI_REASONING_EFFORT`(기본 `low`). 추론 요약은 Responses API로 받아 화면의 "생각 과정"에 보여줍니다.
  조직 인증이 안 된 계정이라 요약이 거절되면 `OPENAI_REASONING_SUMMARY=false`로 끕니다.
- 크레딧이 떨어지면(429 `insufficient_quota`) 분당 한도 초과와 구분해서 "크레딧이 부족합니다"로 안내합니다.
- 대화 이력 예산 기본값은 32,000 토큰입니다. 매 턴 보내는 입력이 곧 비용이라서, 넘으면 앞부분을 요약합니다.

Google(Gemini API) 예시:
```bash
# https://aistudio.google.com 에서 API 키 발급 후 .env:
# LLM_PROVIDER=google, GOOGLE_API_KEY=..., GOOGLE_MODEL=gemma-4-26b-a4b-it
make backend
```
- 무료 티어는 분당/하루 요청 수 한도가 있습니다. 도구를 쓰는 답변은 모델을 여러 번 부르고(한 턴 최대 16단계),
  대화 요약도 한 번 더 부르므로 한도가 생각보다 빨리 찹니다. 내 한도는 AI Studio의 rate limit 화면에서 확인하세요.
  한도를 넘으면(429) 화면에 "사용 한도를 넘었습니다" 안내가 나옵니다. 재시도 횟수는 `GOOGLE_MAX_RETRIES`(기본 2).
- 무료 티어는 입력 내용이 Google 제품 개선에 쓰일 수 있습니다. 개인정보나 회사 자료는 넣지 마세요.
- 대화 이력 예산 기본값은 32,000 토큰입니다(`CONTEXT_MAX_TOKENS`로 변경). 컨텍스트는 26만 토큰이지만 매 턴 전체 이력을 보내면
  분당 토큰 한도에 걸리기 쉬워서, 넘으면 Ollama와 같은 방식으로 앞부분을 요약합니다.
- 추론 요약은 `thinking` 블록으로 와서 화면의 "생각 과정"에 그대로 보입니다(`GOOGLE_INCLUDE_THOUGHTS=false`로 끔).

### 이미지 입력 (VLM)
채팅 입력창에서 이미지 버튼, 붙여넣기, 드래그 앤 드롭으로 첨부합니다(메시지당 최대 4장).
- 프론트엔드가 긴 변 1024px로 줄여서 base64로 전송 → 백엔드가 검증(형식, 5MB, 장수) 후 원본을 첨부 저장소(MinIO `images/<ID>`)에 저장하고, `message_images` 테이블에는 객체 키만 저장
- 대화 상태(checkpoint)에는 원본 대신 이미지 ID 만 담은 참조 블록을 저장 → 턴마다 같은 이미지가 다시 쌓이지 않음
- 모델을 부르기 직전에 참조를 LangChain 표준 이미지 블록(`{"type": "image", "base64", "mime_type"}`)으로 바꿔서 전달 → Anthropic / Google / Ollama 형식으로 자동 변환 (`app/images.py`)
- 이미지는 문서 원본과 달리 보관 기간으로 지우지 않고, 대화를 지울 때 함께 지움 (첨부 저장소가 없으면 이미지 전송은 503)
- 화면에서는 `GET /api/images/{id}`로 이미지를 불러옴
- vision을 지원하지 않는 모델이면 `ENABLE_VISION=false` (이미지 첨부 시 400 응답)

### 컨텍스트 관리
| 설정 | 의미 |
|---|---|
| `OLLAMA_NUM_CTX` | 모델이 한 번에 보는 토큰 수(입력+출력). 클수록 KV 캐시 메모리 증가 |
| `OLLAMA_NUM_PREDICT` | 출력 최대 토큰 (thinking 포함) |
| `OLLAMA_TIMEOUT_SECONDS` | 요청 타임아웃 (기본 300초). Ollama 가 멈췄을 때 무한히 기다리지 않게 함 |
| `CONTEXT_MAX_TOKENS` | 모델에 보낼 대화 이력 예산. 기본값 `NUM_CTX - NUM_PREDICT - 256` |
| `IMAGE_TOKEN_ESTIMATE` | 예산 계산 시 이미지 1장의 토큰 수 (기본 1500) |

Ollama는 `num_ctx`를 넘는 입력을 **오류 없이 앞부분부터 잘라냅니다**. 그래서 `agent` 노드가 호출 전에
`trim_messages`로 최근 대화만 예산 안에 맞춥니다. 토큰 수는 글자 수로 추정하는데, 한국어는 영어보다 글자당 토큰이 2배 이상 많아서
한글 등 ASCII가 아닌 글자는 1.5자당 1토큰으로 따로 셉니다(`orchestration/context.py`의 `make_token_counter`). checkpointer에 저장된 원본 이력은 그대로 남고,
모델에 실제로 들어간 입력은 Langfuse에서 확인할 수 있습니다. (Anthropic은 컨텍스트가 크고 이력을
자르면 캐시가 깨지므로 기본적으로 자르지 않습니다.)

#### 지난 도구 결과 줄이기
도구 결과(웹 검색 한 번에 수천 토큰)는 상태에 남아 이후 모든 턴에 다시 입력됩니다. 그래서 모델에 보낼 때
**마지막 사용자 메시지보다 앞에 있는 도구 결과는 줄여서** 보냅니다(`orchestration/context.py`의 `compact_old_tool_results`).
이번 턴의 결과와 저장된 원본은 그대로입니다.

- 출처가 있는 결과(웹 검색): 호출 인자와 `[번호] 제목 - URL (발행일)` 목록만 남깁니다. 자세한 내용이 다시 필요하면 모델이 다시 검색합니다.
- 그 밖의 긴 결과(500자 초과): 앞부분 300자만 남깁니다. 오류 결과와 짧은 결과는 그대로 둡니다. 도구마다 다른 방식이 필요하면 `compactors`로 넘깁니다.
- 요약 여부도 줄인 크기로 판단합니다. 검색을 4번 한 실제 대화에서 이력이 8,937 → 5,087 토큰으로 줄었습니다.
- 도구 결과는 다음 턴이 시작될 때 한 번만 바뀌므로 Anthropic 프롬프트 캐시도 그 지점에서 한 번만 다시 만들어집니다.
- `COMPACT_TOOL_RESULTS=false`로 끌 수 있습니다.

#### 대화 요약
예산이 있을 때(Ollama·Google 기본, 또는 `CONTEXT_MAX_TOKENS` 지정)는 잘려 나갈 앞부분을 요약해서 남깁니다.

- 턴을 시작할 때 요약하지 않은 이력이 예산의 80%를 넘으면 `summarize` 노드가 실행됩니다.
  최근 이력은 예산의 40% 안에서 원문으로 남기고(항상 사용자 메시지부터), 그 앞을 기존 요약에 합쳐 새 요약을 만듭니다.
- 요약(`summary`)과 요약에 포함된 마지막 메시지 ID(`summarized_until`)는 그래프 상태로 checkpointer에 저장됩니다.
  원본 메시지는 지우지 않습니다.
- `agent` 노드는 요약을 system 프롬프트 끝에 붙이고(`chat/conversation_summary`), 요약 이후의 메시지만 보냅니다.
- 요약은 같은 모델로 하되 추론을 끄고 출력은 1024 토큰으로 제한합니다(`chat/summary`). 요약 토큰은 화면에 나가지 않습니다.
- 요약이 실패하거나 120초를 넘기면 경고 로그만 남기고 기존 방식(잘라내기)으로 답합니다.
- 비율 같은 값은 `orchestration/summary.py`의 상수입니다. 요약이 일어나는 것을 직접 보려면 `CONTEXT_MAX_TOKENS=2000`처럼 작게 잡으세요.

## 구조

```
backend/
├── app/
│   ├── main.py              # 앱, CORS, lifespan (DB 초기화 + 에이전트 등록·런타임 시작/종료)
│   ├── api/                 # HTTP 계층: 인증, 입력 검증, 응답 형식, SSE 변환만
│   │   ├── deps.py          # DB 세션, 요청별 현재 사용자 (FastAPI 의존성)
│   │   ├── schemas.py       # 요청/응답 스키마
│   │   └── routers/         # auth, approvals, admin, documents, projects, chat(/api/chat SSE, 대화, 이미지)
│   ├── conversations/       # 대화 도메인
│   │   ├── service.py       # 대화 조회·삭제, 프로젝트 삭제, 새 턴 준비(검증 + 사용자 메시지 저장)
│   │   ├── turns.py         # 한 턴의 응답 실행: 이벤트 내보내기 + 답변 저장
│   │   └── runs.py          # 대화별 "생성 중" 상태
│   ├── agents/              # 에이전트 정의와 등록 목록(AGENTS)
│   │   └── chat/            # 기본 채팅 에이전트
│   │       ├── graph.py     # LangGraph: agent ⇄ tools, 쓸 도구 이름(TOOL_NAMES)
│   │       └── prompts/     # system.md, summary.md ...
│   ├── orchestration/       # 에이전트 실행 엔진 (어떤 에이전트가 있는지 모름)
│   │   ├── runtime.py       # 에이전트 등록, checkpointer, 스트리밍 실행
│   │   ├── events.py        # 화면으로 보내는 이벤트, 실행 단계 기록
│   │   ├── context.py       # 대화 이력 trim, 프롬프트 동적 변수(오늘 날짜)
│   │   ├── summary.py       # 오래된 대화 요약
│   │   └── tooling.py       # 도구 오류 처리, 반복 제한
│   ├── tools/               # 공용 도구 (registry.py 에 등록, 에이전트가 이름으로 골라 씀)
│   ├── projects/            # 프로젝트: 만들기·조회, 프로젝트 문서, 에이전트에게 줄 프로젝트 정보, 문서 검색
│   ├── documents/           # 첨부·프로젝트 문서: 형식 확인, 파싱(xberg + PaddleOCR, 별도 프로세스), 요소·청크, 정리
│   ├── web/                 # 웹 검색(Tavily), 페이지 읽기
│   ├── auth/                # 로그인: Google OAuth, 세션, 사용자 상태(승인제)
│   ├── images.py            # 채팅 이미지: 저장소 저장, checkpoint 참조 ↔ 원본
│   ├── llm/                 # provider 별 chat model 생성 (모든 에이전트 공용) + fake 모델, 그림 설명, 임베딩
│   ├── prompts/             # 프롬프트 로더 + 공용 조각 (shared/language.md, formatting.md ...)
│   ├── models.py            # SQLAlchemy 모델
│   └── core/                # 인프라: 설정, DB, 첨부 저장소(S3 API), Langfuse, 공용 예외
└── tests/                   # make test
```

**계층 규칙:** 위 계층은 아래 계층을 import 할 수 있지만 반대는 안 됩니다. `make lint`가 `lint-imports`로 검사합니다
(규칙은 `backend/pyproject.toml`의 `[tool.importlinter]`).

```
main → api → conversations → agents → orchestration → tools
     → projects · documents · web · auth · images → llm · prompts → models → core
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
| `chat/project` | `app/agents/chat/prompts/project.md` (프로젝트 대화일 때 system 프롬프트에 덧붙임) |
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
make prompt-sync     # 동기화 (= docker compose exec backend python -m app.prompts.sync)
make prompt-check    # 변경 사항만 확인 (--dry-run)
```

- **include는 합쳐서, 변수는 `{{var}}` 형식으로** 올라갑니다. 그래서 Jinja 문법은 변수 치환과 정적 include만 허용합니다(검증에서 확인).
- **내용이 바뀐 경우에만 새 버전**이 생깁니다. 공용 조각을 고치면 그 조각을 쓰는 모든 프롬프트의 새 버전이 생깁니다.
- **롤백:** Langfuse 화면에서 `production` 라벨을 이전 버전으로 옮기면 재배포 없이 바로 반영됩니다(캐시 60초).
  이후 sync를 실행해도 최신 버전과 내용이 같으면 아무것도 바꾸지 않으므로 롤백 상태가 유지됩니다.
- Langfuse 화면에서 프롬프트를 직접 고치는 것은 실험용으로만 쓰세요. 변수 구성이 코드와 다르면 그 버전은 쓰지 않고 로컬 파일을 사용합니다.

## 실행 단계 표시 (추론 과정, 도구 상태)

응답 위에 모델이 **생각한 과정**과 **도구 실행 상태**를 일어난 순서대로 보여줍니다. 기본은 접혀 있고,
눌러서 추론 내용이나 도구의 입력·결과를 볼 수 있습니다. 실행 단계는 메시지와 함께 저장되어
새로고침해도 남습니다(`messages.steps`, JSONB).

| SSE 이벤트 | 내용 |
|---|---|
| `reasoning` | 추론 과정 조각. Claude 는 요약(`display: summarized`), qwen 등은 원문(`OLLAMA_REASONING=true`) |
| `tool_start` | 모델이 도구 호출을 결정함 (이름, 인자, 표시 문구) |
| `tool_end` | 도구 결과 (최대 2,000자) |

- 도구의 표시 문구는 `app/agents/<에이전트>/tools.py`의 `TOOL_LABELS`에 둡니다.
  예: `get_current_time` → 실행 중 "시간을 확인하는 중", 완료 "시간 확인함". 없으면 기본 문구를 씁니다.
- 응답이 중단되거나 오류로 끝나면, 실행 중이던 도구는 "중단됨" / "실패"로 저장됩니다.

## 동시 요청 처리

한 대화에서는 응답을 하나씩만 생성합니다. 같은 대화의 응답이 두 개 동시에 생성되면 대화 상태가
갈라져 한쪽 대화가 모델의 기억에서 사라지기 때문입니다.

| 상황 | 동작 |
|---|---|
| 같은 창에서 응답 생성 중 입력 | **대기열**에 넣음 (1개까지, 그 이상은 거절). 응답이 끝나면 자동 전송 |
| 응답 중 "중지" | 서버 정리가 끝나면 대기 메시지를 바로 전송 |
| 응답 중 오류 | 자동 전송을 멈추고 "대기 메시지 보내기" 버튼 표시 |
| 다른 창에서 같은 대화에 입력 | 서버가 **409로 거절**. 입력 내용은 입력창에 남고, 안내 상자 표시 후 끝나면 자동 갱신 |
| 생성 중인 대화 삭제 | 409로 거절 |
| 응답 중 / 대기 메시지가 있을 때 다른 대화로 이동 | 막음 |

- 서버는 대화별 "생성 중" 상태를 `app/conversations/runs.py`에서 관리합니다. 지금은 메모리에 두고(백엔드 1개 기준),
  여러 프로세스로 늘리면 같은 인터페이스로 Redis 구현을 만들어 바꿉니다.
- 다른 창의 대화는 지금 폴링(`GET /api/conversations/{id}/status`)으로 확인합니다.
- 다른 창에서 서로 **다른 대화**를 동시에 진행하는 것은 제한하지 않습니다.

### 웹 페이지 읽기 (fetch_url)
모델이 URL을 주면 페이지 본문을 읽습니다. **키 없이도 동작**합니다.

- 페이지를 직접 받아서 [trafilatura](https://trafilatura.readthedocs.io)로 본문, 제목, 발행일을 뽑습니다. 메뉴, 로그인 안내, 광고가 빠집니다.
- 직접 받기가 실패하면(봇 차단, 자바스크립트로 그리는 페이지, PDF 등) `TAVILY_API_KEY`가 있을 때 Tavily extract로 다시 시도합니다.
- 긴 페이지는 `FETCH_MAX_CHARS`(기본 8,000자)씩 나눠 주고, 이어 읽을 `offset`을 알려 줍니다. 같은 페이지는 10분 동안 다시 받지 않습니다.
- 결과는 `<page_content>` 태그로 감싸 지시가 아닌 참고 자료로 표시하고, 답변 아래 출처 카드로 보여 줍니다.

**내부 주소 접속 방지 (SSRF, `app/web/safe_http.py`)**
백엔드는 postgres, Langfuse와 같은 Docker 네트워크에 있으므로, 모델이 내부 서비스를 읽지 못하게 막습니다.
- 접속하는 순간 호스트를 DNS로 풀어서 **모든 IP가 공인 주소일 때만 그 IP로 접속**합니다(루프백, 사설망, link-local과 메타데이터 주소, CGNAT, IPv4를 담은 IPv6 등 차단). 검사한 IP로 접속하므로 DNS rebinding이 통하지 않습니다.
- 리다이렉트는 직접 따라가며 단계마다 다시 검사합니다(최대 5번). 프록시 환경 변수는 쓰지 않습니다.
- 내부 주소로 막힌 URL은 Tavily로도 보내지 않습니다. 응답은 `FETCH_MAX_BYTES`(기본 5MB)까지만 받습니다.

| 설정 | 의미 |
|---|---|
| `ENABLE_FETCH_URL` | 페이지 읽기 도구 사용 여부 (기본 true) |
| `FETCH_MAX_CHARS` | 한 번에 보여줄 최대 글자 수 (기본 8000) |
| `FETCH_TIMEOUT_SECONDS` | 요청 타임아웃 (기본 15초) |
| `FETCH_MAX_BYTES` | 받을 최대 크기 (기본 5MB) |

## 로그인과 접근 제어

Google 계정으로 로그인하고, **관리자가 승인한 사람만** 쓸 수 있습니다.

1. 화면에서 "Google 로 로그인" → 처음이면 "승인 대기" 화면에서 신청 사유를 적어 신청합니다.
2. 관리자(`ADMIN_EMAILS`)에게 승인·거절 링크가 담긴 메일이 갑니다 (48시간, 한 번만, 확인 버튼을 눌러야 처리).
   관리 화면 `/admin` 에서도 승인·거절·정지·정지 해제할 수 있습니다.
3. 승인되면 신청자에게 메일이 가고, 대기 화면이 자동으로 채팅 화면으로 바뀝니다.

설정:

- `.env` 에 `GOOGLE_OAUTH_CLIENT_ID` / `SECRET`, `ADMIN_EMAILS` 를 넣습니다. OAuth 클라이언트의 승인된 리디렉션 URI 는
  `http://localhost:8000/api/auth/google/callback` 입니다 (배포 때는 도메인 주소).
- 알림 메일은 Gmail SMTP 로 보냅니다: `SMTP_USERNAME`(보내는 Gmail), `SMTP_PASSWORD`(**앱 비밀번호**, 계정 비밀번호 아님).
- 처음 로그인한 사람은 "승인 대기" 이고, 채팅·업로드 등 API 는 403 입니다. 관리자 이메일은 바로 승인됩니다.
- 대화·문서·이미지는 **주인만** 볼 수 있습니다. 다른 사람의 ID 로 요청하면 404 를 돌려줍니다 (존재 여부도 숨김).
- 세션은 DB 에 토큰 해시만 저장하고, 쿠키는 `HttpOnly`·`SameSite=Lax`(https 면 `Secure`)입니다. 상태를 바꾸는 요청은 출처(Origin)를 확인합니다.
- `AUTH_REQUIRED=false` 면 로그인 없이 로컬 사용자 한 명으로 동작합니다 (개발용). **다른 사람이 접속하는 환경에서는 반드시 `true`.**

## 첨부 파일 저장소 (MinIO)

첨부 원본은 앱 전용 MinIO(`minio` 서비스)에 저장합니다.

- Langfuse 용 MinIO 와 계정·볼륨을 나눴습니다. Langfuse 없이도 동작하고, Langfuse 를 초기화해도 첨부 원본은 남습니다.
- 백엔드가 시작할 때 버킷(`S3_BUCKET`, 기본 `attachments`)과 보관 기간 규칙(`ATTACHMENT_RETENTION_DAYS`, 기본 30일, 대화 첨부 원본 `documents/` 에만 적용)을 만듭니다.
  프로젝트 문서 원본(`projects/`)과 채팅 이미지(`images/`)는 보관 기간 없이 지울 때까지 둡니다.
  MinIO 에 연결하지 못해도 앱은 시작하고, 첨부만 쓸 수 없습니다.
- 코드는 MinIO 전용 API 가 아니라 S3 API(boto3)만 씁니다. `S3_ENDPOINT_URL`과 키만 바꾸면 AWS S3, Cloudflare R2 등으로 옮길 수 있습니다.

### 첨부 문서

`POST /api/documents`로 문서를 올리면 원본을 저장소에 넣고, 백그라운드에서 텍스트로 바꿉니다.
파싱이 끝난 문서를 `POST /api/chat`의 `document_ids`로 보내면 모델이 읽고 답합니다.
화면에서는 입력창의 첨부 버튼, 붙여넣기, 끌어다 놓기로 올리고, 다 읽을 때까지 파일 칩에 상태가 보입니다.

- **형식**: PDF, DOCX, XLSX, PPTX, HWP, HWPX, TXT, MD, CSV. 확장자가 아니라 파일 내용으로 형식을 확인하고, 압축 폭탄을 막습니다.
- **파싱**: [xberg](https://github.com/kreuzberg-dev/kreuzberg)(Kreuzberg 후속, MIT) + PaddleOCR 한국어 모델.
  - PDF: 본문은 텍스트 레이어이고, 글자가 거의 없는 쪽(스캔본)만 쪽 전체를 OCR 합니다.
    **그림 속 글자는 남길 그림만 하나씩 OCR** 해서 본문과 섞지 않습니다. 쪽 전체 OCR 을 본문과 합치면 차트의 눈금·범례가
    문장 사이에 끼어서, 모델이 눈금 값(예: 세로축 최대 4.6)을 데이터로 읽는 오답이 났기 때문입니다. 그림 속 글자는 프로젝트 문서면
    그림 요소에(그림 설명의 참고 자료와 대체 글자), 대화 첨부면 쪽 끝에 `[그림 속 글자 (OCR, …)]` 표시를 붙여 넣습니다.
    텍스트 레이어는 [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)(Apache-2.0/BSD)로 뽑습니다. xberg 기본 추출은
    "칸 이름 | 값" 이 나란한 표 같은 배치를 여러 단으로 읽어서 줄 순서가 뒤섞였기 때문입니다(회의록의 회의 내용이 섞임).
    그림·표 목록과 OCR 은 xberg 를 씁니다.
  - Office·한글: 문서 안 그림을 OCR 해서 제자리에 넣습니다. 표는 Markdown 표로 남습니다.
  - XML 로 된 `.hwp`(국가법령정보센터 등의 HWPML)는 앱이 본문만 직접 뽑습니다.
- **실행**: 문서마다 새 프로세스에서 시간 제한(`DOCUMENT_PARSE_TIMEOUT_SECONDS`, 기본 3분)을 걸고 파싱합니다.
  파서가 멈추거나 죽어도 백엔드는 영향을 받지 않습니다. 백엔드가 재시작되면 파싱 중이던 문서는 실패로 표시합니다.
- **상태**: `processing` → `ready` / `failed`(실패 사유: 암호 문서, 손상, 쪽 수 초과, 시간 초과 등).
- **정리**: 올렸지만 보내지 않은 문서는 하루(`DOCUMENT_UNSENT_TTL_HOURS`), 보낸 문서는 30일 뒤 지웁니다. 대화를 지우면 그 문서도 지웁니다.
- OCR 모델(약 30MB)은 Docker 이미지를 만들 때 미리 받습니다 (`backend/scripts/prefetch_ocr_models.py`).

**모델이 읽는 방법**
- 보낸 문서는 그 대화와 메시지에 연결되고, **그 대화 안에서만** 읽을 수 있습니다. 한 메시지에 5개까지(`MAX_DOCUMENTS_PER_MESSAGE`) 붙일 수 있고, 파싱 중이거나 실패한 문서는 보낼 수 없습니다.
- 모델에게는 사용자 메시지 뒤에 첨부 목록(이름, 형식, 쪽 수, 글자 수, ID)을 붙여 보냅니다. 화면에 저장하는 메시지에는 붙이지 않습니다.
- 짧은 문서(추정 2,000토큰 이하, `DOCUMENT_INLINE_MAX_TOKENS`)는 본문까지 바로 넣어서 도구 호출을 아낍니다.
- 긴 문서는 `read_document(document_id, offset)` 도구로 8,000자(`DOCUMENT_READ_MAX_CHARS`)씩 나눠 읽습니다. 다른 대화의 문서 ID 를 넣으면 찾을 수 없다고 답합니다.
- 문서 본문은 `<document_content>` 태그로 감싸 지시가 아닌 참고 자료로 표시합니다 (웹 페이지 읽기와 같은 프롬프트 인젝션 방어).

### 프로젝트

문서를 모아 두고 그 안에서 여러 대화를 하는 기능입니다 (Claude 의 프로젝트와 비슷). 사이드바의 "프로젝트"에서 만듭니다.

- 프로젝트 = 이름 + **지시문** + **문서** + 그 안의 **대화들**. 프로젝트 화면에서 보낸 메시지는 그 프로젝트 안의 새 대화가 됩니다.
- 지시문과 문서 목록은 매 응답마다 system 프롬프트 끝에 붙입니다 (`chat/project`). 지시문을 고치면 다음 응답부터 적용됩니다.
  checkpoint 에는 저장하지 않습니다 (실행 설정 `configurable["project"]` 로 넘김).
- **작은 프로젝트**: 문서 합계가 추정 2만 토큰 이하(`PROJECT_INLINE_MAX_TOKENS`, Ollama 는 1만 `OLLAMA_PROJECT_INLINE_MAX_TOKENS`)면
  본문을 모두 system 프롬프트에 넣어 도구 없이 바로 답합니다. 넘으면 목록만 주고 `search_project` 로 찾고 `read_document` 로 읽게 합니다.
- 모델은 `search_project` 로 프로젝트 문서를 검색하고 `read_document` 로 읽습니다. 대상 프로젝트는 서버가 정하므로,
  모델이 다른 프로젝트의 문서 ID 를 넣어도 읽지 못합니다.
- **보관**: 프로젝트 문서는 30일 정리를 받지 않고 지울 때까지 둡니다. 메시지에 첨부할 수는 없습니다.
  프로젝트를 지우면 그 안의 대화·문서·이미지·checkpoint 가 모두 지워집니다 (응답 생성 중인 대화가 있으면 409).
- **용량**: 문서 수는 제한하지 않고, 준비된 문서의 추정 토큰 수 합계로 제한합니다 (`PROJECT_MAX_TOKENS`, 기본 100만).
  꽉 찬 프로젝트에는 올릴 수 없고, 파싱해 보니 넘치는 문서는 "용량 초과" 로 실패 처리합니다.
- **입력 비용**: 문서 본문을 매 응답마다 다시 보내므로, Claude 는 프롬프트 캐시를 켭니다 (요청 최상위 `cache_control`, 5분 유지).
  같은 앞부분(도구 정의, system 프롬프트, 대화 이력)을 다시 보내면 캐시에서 읽어 입력 단가가 0.1배가 됩니다.
  OpenAI 와 Gemini 는 같은 앞부분을 자동으로 캐시합니다.
- **구조화 적재**: 프로젝트 문서는 본문과 함께 **요소**(본문·표·그림, 쪽 번호와 제목 경로 포함)로 나눠
  `document_elements` 에 저장합니다 (`app/documents/elements.py`). 검색(RAG)용 청크를 이 요소로 만듭니다.
  - Office·한글: xberg 문서 구조 트리로 제목 경로를 따라가며 단락·표·그림을 순서대로 모읍니다. PPTX 발표자 노트도 넣습니다.
  - PDF: 본문은 쪽마다(텍스트 레이어, 그림 속 글자는 넣지 않음), 표는 쪽별 표 추출 결과로 만들고, 쪽을 넘는 표에는 앞 표의 머리글을 이어 붙입니다.
    **본문은 문단과 제목으로 나눕니다** (`app/documents/pdf_layout.py`): pdfium 의 줄마다 위치와 글자 크기를 보고, 줄 간격이 문단 안의
    간격의 1.5배(본문 글자 크기 이하)를 넘거나 글자 크기가 달라지면 새 문단으로 나눕니다. 본문보다 1.15배 이상 크고 짧은(2줄·80자 이하)
    문단은 **제목**으로 보고, 크기 순으로 단계를 매겨 뒤따르는 문단의 제목 경로(`보고서 제목 > 1. 요약`)로 씁니다(쪽을 넘어 이어짐).
    그래서 청크가 절 경계에서 나뉘고 임베딩 앞에 제목이 붙으며, 비슷한 내용(회의록의 여러 회의 등)을 구분하기 쉬워집니다.
    **그림은 쪽 안의 제자리**(그림의 세로 가운데보다 아래에 있는 첫 문단 앞)에 넣습니다. xberg 그림과는 같은 쪽·같은 픽셀 크기로 짝을
    짓고, 위치를 모르면(스캔 쪽 등) 쪽 끝에 둡니다. 스캔 쪽은 OCR 본문 하나로 두고 앞 쪽의 제목 경로를 이어받습니다.
  - 그림: 아이콘·로고·장식과 작은 그림, **빈 그림**(거의 한 가지 색이라 아주 작게 압축된 것)은 버립니다. PDF 는 투명한 부분이
    있는 그림을 컬러 그림과 회색조 마스크로 나눠 저장하는데 xberg 가 둘 다 돌려주므로, 같은 쪽·같은 크기의 컬러 그림이 있는
    회색조 그림도 마스크로 보고 버립니다. 나머지(문서당 최대 50장, `VISION_MAX_IMAGES_PER_DOCUMENT`)는
    원본을 `projects/<프로젝트>/images/` 에 저장한 뒤 OpenAI vision 모델(`VISION_MODEL`, 기본 `gpt-6-luna`, 낮은 해상도)로
    **한국어 설명**을 만듭니다. 그림 속 OCR 글자를 함께 넘겨서 낮은 해상도에서 놓치기 쉬운 작은 숫자·라벨을 정확히 읽게 하고,
    설명에 실패한 그림은 그림 속 OCR 글자로 대신합니다(문서는 그대로 준비됩니다).
  - **문서 본문은 요소로 다시 만듭니다** (`elements.compose_content`): 쪽 안의 순서대로 본문과 `[그림] 설명` 이 들어가고, PDF 는 표 내용이
    본문에 이미 있어서 표 요소는 넣지 않습니다. 그래서 `read_document`, 작은 프로젝트의 본문 넣기, 검색이 모두 같은 내용을 봅니다.
  - **다시 처리**: 프로젝트 화면의 "다시 처리" / "모두 다시 처리"(`POST /api/projects/{id}/documents/{doc}/reprocess`,
    `POST /api/projects/{id}/reprocess`)는 저장된 원본으로 처음부터 다시 적재합니다. 파서를 고친 뒤 다시 올리지 않아도 됩니다.
  - 문서 상태는 `processing`(읽는 중) → `analyzing`(그림 분석 중) → `indexing`(검색 준비 중) → `ready` 입니다.
    그림 설명도 용량에 들어갑니다.
  - 대화 첨부 문서는 올리는 시간이 길어지지 않도록 요소를 만들지 않고 그림도 설명하지 않습니다.
- **검색 (RAG)**: 요소를 **청크**로 묶거나 나눠 OpenAI 임베딩(`EMBEDDING_MODEL`, 기본 `text-embedding-3-small`, 1536차원)과 함께
  `document_chunks` 에 저장합니다 (`app/documents/chunking.py`, `app/projects/search.py`).
  - **하이브리드 검색**: 벡터 검색(pgvector HNSW, 코사인 거리)과 키워드 검색(Postgres 전문 검색, GIN)에서 각각 후보 30개
    (`PROJECT_SEARCH_CANDIDATES`)를 뽑고, 순위를 RRF(`1 / (k + 순위)` 의 합, `PROJECT_SEARCH_RRF_K=60`)로 합쳐 상위 8개를 돌려줍니다.
    벡터는 뜻이 비슷한 글을, 키워드는 코드명·숫자·고유명사처럼 글자가 같아야 하는 것을 잘 찾습니다.
  - 키워드 토큰(`app/documents/keywords.py`): 기본 전문 검색은 "부산의" 와 "부산" 을 다른 단어로 봐서, 한글 낱말은 낱말과
    **두 글자 조각**("부산의" → 부산의, 부산, 산의)으로 나눠 `to_tsvector('simple', ...)` 에 넣습니다. 형태소 분석기나 확장이 필요 없습니다.
  - 청크: 같은 제목 아래의 작은 본문은 목표 500 토큰까지 묶고, 800 토큰이 넘는 본문은 문단 → 줄 → 문장 순으로 나눠 80 토큰씩 겹칩니다.
    표는 행 단위로 나누며 조각마다 머리글을 붙이고, 그림은 설명 하나가 청크 하나입니다 (`CHUNK_*` 설정).
    청크는 문서 본문·문서 보기와 같은 규칙으로 요소를 고릅니다. 그래서 **PDF 는 표 청크를 만들지 않습니다**: 표 내용이 본문
    (텍스트 레이어) 청크에 이미 있어서, 넣으면 같은 내용이 검색 결과 두 자리를 차지했습니다.
  - **문서 하나로 좁혀 찾기**: `search_project` 의 `document_id`(선택)를 넣으면 그 문서 안에서만 찾습니다. "설계서에서 ~" 처럼
    한 문서를 묻는 질문에 씁니다. 프로젝트 조건은 그대로라 다른 프로젝트의 문서 ID 는 찾지 못하고, 목록에 없는 ID 면 안내를 돌려줍니다.
  - 임베딩 입력 앞에는 "문서 이름 | 제목 경로 | 쪽" 을 붙여 맥락을 줍니다. 결과는 문서 이름·쪽·제목 경로와 함께 모델에게 주고,
    화면에는 답변 아래 **출처 카드**로 보여줍니다 (그림 결과는 미리보기, `GET /api/projects/{id}/images/{element_id}`).
  - **문서 보기 패널**: 출처 카드를 누르면 오른쪽 패널에 문서의 추출 내용을 보여주고 근거가 된 부분으로 스크롤해 강조합니다
    (`GET /api/projects/{id}/documents/{doc}/view?chunk_id=…`, `components/DocumentViewer.tsx`). 프로젝트 화면의 문서 칩을 눌러도
    같은 패널이 열립니다(원본은 패널 위 "원본 받기"). 원본 모양이 아니라 **모델이 읽은 내용**(문서 본문과 같은 규칙으로 고른 요소,
    그림은 미리보기와 설명)이라서 답이 정말 거기서 나왔는지 바로 확인할 수 있고, 형식과 관계없이 같은 화면입니다.
    청크는 자기를 만든 요소 ID(`element_ids`)를 기억해서 그 블록을 강조하고, 긴 본문을 나눈 조각이면 블록 안의 그 부분을
    형광펜(CSS Custom Highlight API)으로 한 번 더 표시합니다. 이 기능 전에 만든 청크는 같은 쪽에서 내용이 겹치는 블록을 찾고,
    청크 ID 가 없는 예전 출처 카드는 그 쪽으로만 이동합니다. PDF 표 청크는 표가 본문에 들어 있어 따로 보이지 않으므로 그 쪽으로 이동합니다.
  - 임베딩에 실패하면 청크만 저장하고 문서는 준비됩니다. 빠졌거나 다른 모델로 만든 임베딩은 검색할 때 다시 만듭니다
    (모델을 바꿔도 자동으로 다시 임베딩). 빠진 키워드 토큰도 검색할 때 채웁니다. 검색 기능 전에 올린 프로젝트 문서는
    앱이 시작할 때 청크를 만듭니다.
  - OpenAI 키가 없거나 임베딩 요청이 실패하면 키워드 검색만 씁니다. `EMBEDDING_PROVIDER=fake` 는 네트워크 없이 도는
    글자 조각 해시 임베딩입니다 (테스트·개발용).
  - Postgres 는 `postgres:17-alpine` 에 pgvector 를 빌드해 넣은 이미지를 씁니다 (`docker/postgres/Dockerfile`). 공식 pgvector 이미지는
    Debian(glibc) 기반이라 기존 alpine(musl) 데이터 디렉터리에 붙이면 문자열 정렬 규칙이 달라질 수 있어서입니다.

## DB 마이그레이션 (Alembic)

앱 테이블(`conversations`, `messages`, `message_images`)은 Alembic으로 관리합니다.
**백엔드가 시작할 때 최신 마이그레이션을 자동으로 적용**하므로, 평소에는 신경 쓸 일이 없습니다.

모델(`backend/app/models.py`)을 바꿨을 때:
```bash
make db-revision m="add tags to conversations"   # 변경 내용을 비교해 마이그레이션 파일 생성
# backend/migrations/versions/ 에 생긴 파일을 확인·수정한 뒤
make backend                                      # 재시작하면 자동 적용 (또는 make db-upgrade)
```

| 명령 | 설명 |
|---|---|
| `make db-status` | 현재 리비전과 이력 |
| `make db-check` | 모델과 마이그레이션이 어긋났는지 검사 |
| `make db-downgrade` | 한 단계 되돌리기 |

- LangGraph checkpointer 테이블(`checkpoints` 등)은 LangGraph가 자체적으로 관리하므로 Alembic 관리 대상에서 제외됩니다.
- Alembic 도입 전에 만든 DB는 첫 기동 시 기준 리비전(`0001`)으로 표시만 하고 테이블은 건드리지 않습니다.
- **대화를 삭제하면 그 대화의 checkpointer 데이터도 함께 삭제됩니다.** 삭제 중 실패해서 남은 데이터는 다음 기동 때 정리됩니다.

## 에이전트 / 도구 추가하기

**도구:** `app/tools/<주제>.py`에 도구를 만들고 `app/tools/registry.py`의 `TOOLS`에 등록한 뒤,
쓸 에이전트의 `TOOL_NAMES`(예: `app/agents/chat/graph.py`)에 이름을 넣으면 그래프에 연결됩니다.
API 키처럼 설정이 필요한 도구는 등록할 때 만드는 함수가 설정을 보고 `None`을 돌려주면 빠집니다(키가 없으면 넣지 않음).

```python
@tool(parse_docstring=True)
def search_docs(query: str) -> str:
    """Search internal documents.

    Args:
        query: Search keywords.
    """
    ...

# app/tools/registry.py
TOOLS["search_docs"] = ToolSpec(
    lambda settings: search_docs, ToolLabel(running="문서를 검색하는 중", done="문서 검색함")
)

# app/agents/chat/graph.py
TOOL_NAMES = ("get_current_time", ..., "search_docs")
```

- 도구의 docstring(영어)은 모델에게 도구 설명으로 전달됩니다. 언제 쓰고 언제 쓰지 말아야 하는지까지 적으세요.
- 도구 안에서 예외가 나도 응답 전체가 실패하지 않습니다. `ToolNode`가 예외를 오류 결과로 바꾸고 화면에는 "실패"로 표시합니다.
  모델에게 알려줄 문구가 있으면 `app.core.errors.ToolError("짧은 영어 문구")`를 던지세요. 그 외 예외는 원문을 로그에만 남기고 일반 문구를 줍니다.
- 한 턴에 그래프 단계는 16번까지입니다(`tooling.RECURSION_LIMIT`, 도구 호출 약 7번). 넘으면 사용자에게 안내 문구를 보여줍니다.

### 웹 검색 (Tavily)
`.env`에 `TAVILY_API_KEY`를 넣으면 `web_search` 도구가 켜집니다. 키는 [app.tavily.com](https://app.tavily.com)에서 받으며, 무료 플랜은 매월 1,000크레딧입니다.

| 설정 | 의미 |
|---|---|
| `TAVILY_API_KEY` | 비우면 웹 검색 도구를 등록하지 않음 |
| `TAVILY_SEARCH_DEPTH` | `advanced`(기본, 2크레딧, 관련 본문을 더 잘 뽑음) / `basic`(1크레딧) |
| `WEB_SEARCH_MAX_RESULTS` | 검색 한 번에 받을 결과 수 (기본 5) |
| `WEB_SEARCH_TIMEOUT_SECONDS` | 요청 타임아웃 (기본 20초) |

- 모델이 검색 조건을 정합니다: `query`, `topic`(`general` / `news`, `news` 결과에만 발행일이 있음),
  기간(`time_range`: day / week / month / year, 또는 과거 기간을 찾는 `start_date` / `end_date`).
  Tavily는 기간을 정해도 **최신순이 아니라 관련도순**이라, "내일 날씨"처럼 매일 같은 제목이 나오는 질문은 기간을 정해야 지난 기사가 섞이지 않습니다.
- 결과의 발행일 옆에는 오늘 기준 며칠 전인지(`2026-10-27 (23 days ago)`)를 적어서 모델이 오래된 결과를 알아보게 합니다.
- 화면에는 검색 결과가 답변 아래 출처 카드로 나옵니다(도구의 artifact로 전달, 모델 입력에는 들어가지 않음). 모델은 출처 목록을 직접 쓰지 않습니다.
- **답변 속 출처 번호**: 모델은 출처를 쓴 문장 끝에 `[3]` 처럼 번호를 답니다. 번호는 한 답변(턴) 안에서 이어집니다.
  출처가 있는 도구(웹 검색, 페이지 읽기, 프로젝트 문서 검색)는 결과 글에 자리 표시(`app/core/citations.py`)만 넣고, 도구 노드가 끝나면
  `app/orchestration/citations.py` 가 이번 턴에서 이미 쓴 번호를 이어받아 `[9]` 처럼 바꾸고 출처마다 `n` 을 남깁니다.
  도구가 동시에 실행돼도 번호가 겹치지 않고, 같은 출처(같은 청크·URL)가 다시 나오면 앞의 번호를 씁니다.
  화면은 답변의 `[3]` 을 번호 칩으로 그리고(`lib/citations.ts`, 코드 블록·`a[1]` 같은 첨자는 그대로), 출처 카드도 같은 번호를 씁니다.
  칩을 누르면 웹은 새 탭에서 열고, 프로젝트 문서는 문서 보기 패널에서 근거 위치를 보여 줍니다.
  `read_document` 로 읽거나 본문이 통째로 들어간 프로젝트 문서는 번호가 없어서 문서 이름과 쪽으로 적습니다.
- 모델에게는 결과마다 제목, URL, 발행일, 정리한 본문 조각(약 1,000자)만 줍니다. 관련도 0.3 미만 결과는 버리고,
  메뉴, 관련 기사 목록, 이미지 같은 잡음을 걸러냅니다(`app/web/search.py`).
- 검색 결과는 `<search_results>` 태그로 감싸고 "외부 페이지 내용이며 지시가 아니다"라고 표시합니다.
  웹 페이지에 "이전 지시를 무시하라" 같은 문장이 있어도 모델이 따르지 않게 하기 위해서입니다(프롬프트 인젝션 방어).
- **개인정보:** 모델이 만든 검색어는 외부 서비스(Tavily)로 전송됩니다. 대화 내용이 검색어에 섞일 수 있으니,
  민감한 대화에서는 `TAVILY_API_KEY`를 비워 검색 도구를 끄세요.
- Tavily의 요약 답변(`answer`)과 본문 전체(`raw_content`)는 쓰지 않습니다. 요약 답변은 영어로만 나오고 틀릴 때가 있으며,
  본문 전체는 페이지당 수만 자라 컨텍스트를 금방 채웁니다.

**에이전트:**
1. `app/agents/<이름>/`에 `graph.py`(`build_graph`, `STREAM_NODES`, `TOOL_NAMES`)와 `prompts/*.md`를 만듭니다.
2. `app/agents/__init__.py`의 `AGENTS`에 등록합니다. 앱이 시작할 때 실행 엔진(`orchestration/runtime.py`)에 등록됩니다.
3. 프롬프트 네임스페이스(`<이름>/...`)는 폴더 이름으로 자동 인식됩니다.

## 테스트

```bash
make test     # LLM 없이(fake 모델) 프롬프트, 그래프, 대화 기억, 잘라내기 검증
```

## API

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/models` | 고를 수 있는 모델 목록과 기본 모델 ID |
| POST | `/api/chat` | `{conversation_id?, project_id?, message, images?: [{mime_type, data}], document_ids?, model?}` → SSE 이벤트 `meta` / `delta` / `done` / `error`. 같은 대화가 생성 중이면 409. `project_id` 는 새 대화를 그 프로젝트 안에 만든다 |
| GET | `/api/conversations` | 대화 목록 |
| GET | `/api/conversations/{id}` | 대화 + 메시지 + 생성 중 여부 |
| GET | `/api/conversations/{id}/status` | 생성 중 여부 (다른 창 확인용) |
| DELETE | `/api/conversations/{id}` | 대화 삭제 (첨부 문서 포함, 생성 중이면 409) |
| GET | `/api/images/{id}` | 첨부 이미지 원본 |
| POST | `/api/documents` | 문서 올리기 (multipart `file`) → `processing` 상태로 201. 너무 크면 413, 지원하지 않는 형식이면 415 |
| GET | `/api/documents/{id}` | 문서 정보와 파싱 상태 (`processing` / `ready` / `failed`, 실패 사유, 쪽 수, 글자 수) |
| GET | `/api/documents/{id}/file` | 원본 내려받기 (보관 기간이 지났으면 410) |
| DELETE | `/api/documents/{id}` | 보내지 않은 첨부 문서 지우기 (이미 보낸 문서나 프로젝트 문서면 409) |
| GET | `/api/projects` | 프로젝트 목록 |
| POST | `/api/projects` | 프로젝트 만들기 `{name, instructions?}` |
| GET | `/api/projects/{id}` | 프로젝트 + 문서 + 대화 + 용량(`used_tokens`, `max_tokens`) |
| PATCH | `/api/projects/{id}` | 이름·지시문 바꾸기 (보낸 값만) |
| DELETE | `/api/projects/{id}` | 프로젝트와 그 안의 대화·문서 삭제 (생성 중인 대화가 있으면 409) |
| POST | `/api/projects/{id}/documents` | 프로젝트 문서 올리기 (multipart `file`). 용량이 꽉 찼으면 409 |
| DELETE | `/api/projects/{id}/documents/{doc_id}` | 프로젝트 문서 지우기 |
| POST | `/api/projects/{id}/documents/{doc_id}/reprocess` | 프로젝트 문서 다시 처리 (처리 중이면 409) |
| POST | `/api/projects/{id}/reprocess` | 프로젝트 문서 모두 다시 처리 (처리 중이거나 원본이 없는 문서는 건너뜀) |
| GET | `/api/projects/{id}/documents/{doc_id}/view` | 프로젝트 문서의 추출 내용 (문서 보기 패널). `?chunk_id=` 를 주면 강조할 위치도 |
| GET | `/api/projects/{id}/images/{element_id}` | 프로젝트 문서 속 그림 원본 (출처 카드·문서 보기 패널) |
| GET | `/api/auth/me` | 로그인 설정과 내 정보 (로그인 전에도 200, `user: null`) |
| GET | `/api/auth/google/login` | Google 로그인 시작 (리디렉션) |
| GET | `/api/auth/google/callback` | Google 로그인 콜백 → 세션 쿠키 → 화면으로 |
| POST | `/api/auth/logout` | 로그아웃 |
| POST | `/api/auth/request` | 가입 신청 `{reason}` → 관리자에게 승인·거절 링크 메일 (10분에 한 번) |
| GET·POST | `/api/auth/approvals/{token}` | 메일의 승인·거절 링크: 확인 화면(GET) → 처리(POST) |
| GET | `/api/admin/users` | (관리자) 사용자 목록 |
| POST | `/api/admin/users/{id}/{approve\|reject\|suspend\|reactivate}` | (관리자) 사용자 상태 변경 |
| GET | `/health` | 헬스 체크 |

## 다음 단계

- 장기 기억 메모리 (LangGraph Store 등)
- Langfuse 데이터셋 + 평가(LLM-as-judge)로 프롬프트 버전 간 품질 비교
- CI에서 main 머지 시 프롬프트 자동 동기화
- 이미지를 object storage(MinIO 등)에 두고 상태에는 참조만 저장 (지금은 checkpoint 에 base64 가 반복 저장됨)
- 인증, rate limiting
- Langfuse 활용: 프롬프트 관리, 평가(datasets / LLM-as-judge), 비용 대시보드
