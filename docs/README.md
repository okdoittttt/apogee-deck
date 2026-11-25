# 설계 문서

okdoitttt 의 구조와 동작을 그림과 함께 정리한 문서입니다. 처음 실행한다면 [실행 환경 설정](guides/setup.md)부터 보세요.

## 읽는 순서

1. [전체 아키텍처](architecture/00-overview.md): 시스템이 무엇과 연결되고, 어떤 컨테이너로 나뉘는지
2. [계층 규칙](architecture/layers.md): 백엔드 모듈이 서로 어떤 방향으로만 의존하는지
3. [데이터 저장 위치](data/storage-map.md): 무엇이 어디에 저장되고 언제 지워지는지
4. [한 턴의 흐름](features/chat-turn.md): 다른 기능이 모두 이어지는 중심 흐름. 그다음 관심 있는 기능부터

## 목차

| 구분 | 문서 | 내용 | 상태 |
|---|---|---|---|
| 아키텍처 | [00-overview.md](architecture/00-overview.md) | 시스템 컨텍스트, 컨테이너, 백엔드 구성, 에이전트 그래프 | 작성됨 |
| | [layers.md](architecture/layers.md) | import 계층 규칙과 모듈별 책임 | 작성됨 |
| 데이터 | [erd.md](data/erd.md) | DB 테이블 관계 (영역별 ERD), 인덱스, 삭제 규칙 | 작성됨 |
| | [storage-map.md](data/storage-map.md) | Postgres 테이블, checkpointer, Store, MinIO 에 나뉜 데이터와 삭제 시점 | 작성됨 |
| 기능 | [chat-turn.md](features/chat-turn.md) | 한 턴의 흐름: API → 실행 → 그래프 → SSE | 작성됨 |
| | [streaming-runs.md](features/streaming-runs.md) | 백그라운드 실행, 재연결, 중지, 동시 요청 | 작성됨 |
| | [context-management.md](features/context-management.md) | 이력 자르기, 지난 도구 결과 줄이기, 요약 | 작성됨 |
| | [tool-decision.md](features/tool-decision.md) | 결정 모델(d3)로 턴마다 도구 그룹 고르기 | 작성됨 |
| | [llm-providers.md](features/llm-providers.md) | 모델 목록, provider 별 모델 생성, 그래프 캐시 | 작성됨 |
| | [memory.md](features/memory.md) | 장기 기억: 프로필, 기억, recall | 작성됨 |
| | [documents.md](features/documents.md) | 첨부 문서: 형식 확인, 파싱, 상태 | 작성됨 |
| | [document-editing.md](features/document-editing.md) | 문서 수정·생성: TXT·MD·CSV·DOCX 수정본, 새 문서(TXT·MD·CSV), 파일 칩 | 작성됨 |
| | [projects-rag.md](features/projects-rag.md) | 프로젝트 문서: 요소, 청크, 하이브리드 검색 | 작성됨 |
| | [auth-approval.md](features/auth-approval.md) | Google 로그인, 승인제, 관리자 화면 | 작성됨 |
| | [images.md](features/images.md) | 채팅 이미지 저장과 checkpoint 참조 | 작성됨 |
| | [web-fetch.md](features/web-fetch.md) | 웹 검색, 페이지 읽기, SSRF 방어 | 작성됨 |
| | [prompts.md](features/prompts.md) | 프롬프트 파일, 검증, Langfuse 동기화 | 작성됨 |
| | [feedback-tracing.md](features/feedback-tracing.md) | 👍/👎 피드백과 Langfuse 점수 | 작성됨 |
| | [citations.md](features/citations.md) | 출처 번호, 출처 카드, 문서 보기 패널 | 작성됨 |
| | [ui-routing.md](features/ui-routing.md) | 화면 주소와 History API 라우팅 | 작성됨 |
| 참고 | [api.md](api.md) | HTTP API 와 SSE 이벤트 | 작성됨 |
| 가이드 | [setup.md](guides/setup.md) | 실행 환경 설정: LLM, 로그인, 선택 기능, 외부 접속 | 작성됨 |
| | [development.md](guides/development.md) | 개발: 명령, 마이그레이션, 도구·에이전트 추가, 테스트 | 작성됨 |
| 결정 | [decisions/](decisions/README.md) | 설계 결정 기록 (ADR) 11개 | 작성됨 |

## 작성 규칙

**형식.** 문서는 Markdown 으로 쓰고, 그림은 ` ```mermaid ` 코드 블록으로 넣습니다. GitHub 는 `.md` 안의 mermaid 블록을
그림으로 보여 주지만 `.mmd` 파일은 그림으로 보여 주지 않습니다. 그림 바로 아래에 설명을 함께 둘 수 있다는 점도 이유입니다.

**기능 문서의 틀.** 아래 순서를 따르고, 그 기능에 필요 없는 칸은 뺍니다.

```markdown
# <기능 이름>

> 한 줄 요약.
> 관련 코드: `backend/app/...`, `frontend/src/...`

## 사용자 관점      사용자가 직접 쓰는 기능일 때 (유스케이스)
## 구조             관여하는 모듈과 저장소 (flowchart)
## 흐름             요청 한 번이 지나가는 순서 (sequenceDiagram)
## 상태             상태가 바뀌는 대상이 있을 때 (stateDiagram-v2)
## 주요 타입        클래스 구조가 중요할 때 (classDiagram)
## 데이터           쓰는 테이블과 저장소 키 (data/ 문서로 링크)
## 설계 결정        이렇게 만든 이유. 길면 decisions/ 에 ADR 로 쓰고 링크
```

**그림 종류.**

| 보여 줄 것 | mermaid 문법 |
|---|---|
| 시스템, 컨테이너, 모듈 구성 | `flowchart` (`subgraph` 로 경계 표시) |
| 유스케이스 | `flowchart LR` (mermaid 에 유스케이스 전용 문법이 없어서, 왼쪽에 사용자, 오른쪽에 기능을 둥근 노드로 둠) |
| 요청 순서 | `sequenceDiagram` |
| 상태 변화 | `stateDiagram-v2` |
| 클래스와 타입 | `classDiagram` |
| DB 테이블 | `erDiagram` |

**mermaid 주의점.** GitHub 에서 그림이 깨지지 않도록 지킵니다.

- 공백, 괄호, 한글이 들어간 라벨은 큰따옴표로 감쌉니다: `A["백엔드 (FastAPI)"]`
- 선 라벨은 `-->|라벨|` 또는 `-- "라벨" -->` 로 씁니다.
- 라벨에 `<`, `>` 를 그대로 쓰지 않습니다. 줄바꿈 `<br/>` 만 예외입니다.
- `end`, `graph`, `call`, `class`, `style`, `subgraph` 같은 mermaid 예약어는 노드 ID 로 쓰지 않습니다.

**코드와 맞추기.** 문서 맨 위의 "관련 코드" 줄에 코드 경로를 적습니다. 그 코드를 고칠 때 이 문서도 함께 고칩니다.

**검사.** 문서를 고친 뒤 `make docs-check` 를 돌립니다. mermaid 문법, 링크와 앵커, ERD 가 모델과 같은지 검사합니다.
