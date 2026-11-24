# okdoitttt

LangGraph 기반 LLM 에이전트 웹앱입니다. Claude 의 채팅·프로젝트 기능과 비슷한 개인 학습 프로젝트입니다.

- 여러 provider 의 모델을 화면에서 골라 대화 (Anthropic, OpenAI, Google, Ollama)
- 웹 검색과 페이지 읽기, 출처 번호가 달린 답변
- 문서 첨부(PDF, Office, 한글)와 프로젝트 문서 검색(하이브리드 RAG)
- 대화와 대화 사이의 장기 기억
- Google 로그인과 관리자 승인제
- Langfuse 트레이싱, 프롬프트 버전 관리, 👍/👎 피드백 점수

```
브라우저 ──▶ frontend (Next.js)            화면
   │
   └── REST · SSE ──▶ backend (FastAPI)
                         ├─ LangGraph 에이전트 ──▶ LLM provider
                         ├─ postgres + pgvector ── 앱 테이블 · 대화 상태(checkpointer) · 기억(Store)
                         ├─ minio ─────────────── 첨부 원본 · 이미지
                         └─ Langfuse (선택) ────── 트레이스 · 프롬프트 · 점수
```

자세한 구조는 [전체 아키텍처](docs/architecture/00-overview.md)에 있습니다.

## 빠른 시작

```bash
cp .env.example .env    # LLM 키, 로그인 설정 등
make up                 # 전체 실행
```

| 서비스 | 주소 |
|---|---|
| 채팅 화면 | http://localhost:3000 |
| 백엔드 API 문서 | http://localhost:8000/docs |
| Langfuse | http://localhost:3100 |
| MinIO 콘솔 | http://localhost:9101 |

`.env` 에 무엇을 채워야 하는지는 [실행 환경 설정](docs/guides/setup.md)에 있습니다.
LLM 키 없이 먼저 띄워 보려면 `LLM_PROVIDER=fake`, `AUTH_REQUIRED=false` 로 두세요.

## 자주 쓰는 명령

`make` 만 입력하면 전체 목록이 나옵니다.

| 명령 | 설명 |
|---|---|
| `make up` / `make down` | 전체 실행 / 중지 (데이터 유지) |
| `make backend` / `make frontend` | 해당 서비스만 다시 빌드·재시작 |
| `make logs` / `make ps` | 백엔드 로그 / 컨테이너 상태 |
| `make test` / `make lint` | 백엔드 테스트(LLM 없이 fake 모델) / 린트·계층 검사·프론트엔드 빌드 |
| `make db-revision m="..."` | 모델 변경으로 마이그레이션 만들기 |
| `make prompt-sync` | 프롬프트를 Langfuse 에 동기화 |

`docker compose` 를 직접 쓰지 말고 `make` 를 쓰세요. 커밋 해시를 프롬프트 버전에 기록하기 위해서입니다.
개발 방법은 [개발 가이드](docs/guides/development.md)에 있습니다.

## 문서

설계 문서는 [`docs/`](docs/README.md)에 있습니다.

| 문서 | 내용 |
|---|---|
| [전체 아키텍처](docs/architecture/00-overview.md) · [계층 규칙](docs/architecture/layers.md) | 시스템 구성, 컨테이너, 백엔드 모듈 의존 방향 |
| [ERD](docs/data/erd.md) · [데이터 저장 위치](docs/data/storage-map.md) | DB 테이블, 데이터가 어디에 있고 언제 지워지는지 |
| [기능 문서](docs/README.md#목차) | 한 턴의 흐름, 실행·재연결, 컨텍스트 관리, 기억, 문서, 프로젝트 RAG 등 |
| [API](docs/api.md) | HTTP API 와 SSE 이벤트 |
| [설계 결정 (ADR)](docs/decisions/README.md) | 왜 이렇게 만들었는지 |
| [실행 환경 설정](docs/guides/setup.md) · [개발 가이드](docs/guides/development.md) | 설정, 마이그레이션, 도구·에이전트 추가, 테스트 |

## 코드 구조

```
backend/app/
├── main.py          앱 조립, lifespan
├── api/             HTTP 계층: 라우터, 스키마, 인증 의존성, SSE
├── conversations/   대화, 턴 실행, 생성 중 상태
├── agents/          에이전트 정의 (chat 그래프, 프롬프트)
├── orchestration/   실행 엔진: 스트리밍, 이벤트, 컨텍스트, 요약, 도구 결정
├── tools/           공용 도구 등록 목록
├── projects/  documents/  web/  auth/  images.py  memory/    도메인 기능
├── llm/  prompts/   모델 생성, 프롬프트 로더
├── models.py        SQLAlchemy 모델
└── core/            설정, DB, 저장소, 트레이싱
frontend/src/
├── app/             화면 (page.tsx 하나 + admin)
├── components/      화면 구성 요소
└── lib/             API 호출, 주소, 출처 번호 등
```

위 계층은 아래 계층만 import 할 수 있고, `make lint` 가 검사합니다. 규칙은 [계층 규칙](docs/architecture/layers.md)에 있습니다.
