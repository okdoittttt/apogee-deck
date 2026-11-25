# HTTP API

> 백엔드가 여는 HTTP 엔드포인트 전체 목록과 SSE 이벤트.
> 관련 코드: `backend/app/main.py`, `backend/app/api/routers/*.py`, `backend/app/api/deps.py`, `backend/app/api/schemas.py`

이 문서는 경로, 요청 모양, 권한, 눈여겨볼 상태 코드만 적습니다. 동작은 각 기능 문서에 있습니다.
실행 중인 백엔드의 `http://localhost:8000/docs` 에서 FastAPI 가 만든 스키마도 볼 수 있습니다.

## 공통 규칙

**권한.** 표의 권한 칸은 라우터가 받는 의존성(`app/api/deps.py`)을 따릅니다.

| 권한 | 의존성 | 실패하면 |
|---|---|---|
| 없음 | 없음 | 쿠키 없이도 호출할 수 있다 |
| 로그인 | `CurrentUser` | 세션 쿠키가 없거나 만료되면 401 |
| 승인된 사용자 | `ApprovedUser` | 401, 또는 상태가 `approved` 가 아니면 403. `detail` 은 `{"code": 상태, "message": 문구}` |
| 관리자 | `AdminUser` | 위 조건에 더해 관리자가 아니면 403 |

- `AUTH_REQUIRED=false` 면 모든 요청을 로컬 사용자 한 명(승인된 관리자)으로 처리합니다. 자세한 내용은 [auth-approval.md](features/auth-approval.md#로컬-개발-모드)에 있습니다.
- `POST`, `PUT`, `PATCH`, `DELETE` 요청에 허용 목록 밖의 `Origin` 헤더가 있으면 403 으로 거절합니다(`app.main.check_origin`, CSRF 방어). `Origin` 이 없는 요청(curl 등)은 통과합니다.
- "내 것이 아니면 404" 는 다른 사용자의 대화, 문서, 프로젝트를 있는지조차 알려 주지 않는다는 뜻입니다.
- 요청 본문이 스키마에 맞지 않으면 FastAPI 기본 동작대로 422 입니다. 아래 표에는 따로 적지 않습니다.

## 채팅과 대화

라우터: `app/api/routers/chat.py` (prefix `/api`). 흐름은 [chat-turn.md](features/chat-turn.md), 실행·재연결·중지는 [streaming-runs.md](features/streaming-runs.md)에 있습니다.

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/info` | 화면 표시용 정보: `provider`, `model`, `vision`, `documents` (`enabled`, `max_bytes`, `max_per_message`, `extensions`) | 승인된 사용자 |
| GET | `/api/models` | 고를 수 있는 모델 목록(`models`)과 기본 모델 ID(`default`). [llm-providers.md](features/llm-providers.md) | 승인된 사용자 |
| POST | `/api/chat` | 본문 `{conversation_id?, project_id?, message?, images?: [{mime_type, data}], document_ids?, model?}`. `message`, `images`, `document_ids` 중 하나는 있어야 한다. 응답은 SSE ([아래](#sse-이벤트)). 같은 대화가 생성 중이면 409. 이미지 입력이 꺼져 있거나 쓸 수 없는 모델이거나 문서가 너무 많으면 400. 이미지를 보냈는데 저장소가 없으면 503. 붙일 수 없는 문서면 404/409/400 | 승인된 사용자 |
| GET | `/api/conversations` | 내 대화 목록, 최신순 최대 100개 | 승인된 사용자 |
| GET | `/api/conversations/{conversation_id}` | 대화, 메시지(이미지 ID, 실행 단계, 첨부 문서 정보, 피드백 포함), 생성 중 여부 `running`. 없으면 404 | 승인된 사용자 |
| GET | `/api/conversations/{conversation_id}/status` | `{running}`. 다른 창에서 생성 중인 응답이 끝났는지 확인할 때 쓴다 | 승인된 사용자 |
| GET | `/api/conversations/{conversation_id}/stream` | 진행 중이거나 막 끝난 응답에 다시 붙는다. SSE. 실행 기록이 없으면 204 ([다시 붙기](#다시-붙기)) | 승인된 사용자 |
| POST | `/api/conversations/{conversation_id}/stop` | 진행 중인 응답을 멈춘다. 저장이 끝난 뒤 204 를 돌려준다. 생성 중이 아니면 아무 일도 하지 않는다 | 승인된 사용자 |
| PUT | `/api/conversations/{conversation_id}/messages/{message_id}/feedback` | 본문 `{value: 1 \| -1 \| null, comment?}` (의견 최대 2,000자). `null` 이면 지운다. 응답 `{feedback, comment, scored}`. [feedback-tracing.md](features/feedback-tracing.md) | 승인된 사용자 |
| DELETE | `/api/conversations/{conversation_id}` | 대화, checkpoint, 첨부 문서와 이미지를 지운다. 204. 생성 중이면 409 | 승인된 사용자 |
| GET | `/api/images/{image_id}` | 채팅 이미지 원본. 오래 캐시한다(`immutable`). [images.md](features/images.md) | 승인된 사용자 |

`images` 는 한 메시지에 4장까지, 형식은 `image/jpeg`, `image/png`, `image/webp`, `image/gif` 이고 `data` 는 data URL 접두어 없는 base64 입니다.

### SSE 이벤트

`POST /api/chat` 과 `GET /api/conversations/{id}/stream` 은 같은 이벤트를 보냅니다. 모든 이벤트에 `id:` 줄로 1부터 번호가 붙습니다.

| 이벤트 | `data` |
|---|---|
| `meta` | `conversation_id`. 항상 첫 이벤트 |
| `reasoning` | `text`. 추론 과정 조각 |
| `tool_start` | `id`, `name`, `label`, `done_label`, `args` |
| `tool_end` | `id`, `output`, `status`, `sources`, `documents` (도구가 만든 수정본·새 문서, [문서 수정과 생성](features/document-editing.md#화면까지)) |
| `delta` | `text`. 답변 텍스트 조각 |
| `done` | `stop_reason`, `model`, `message_id`. 정상 종료 |
| `error` | `message`, `error_id`, `transient`. 시간 상한을 넘겨 멈췄을 때는 `error_id` 가 없다 |
| `stopped` | `{}`. 사용자가 중지했다 |

한 응답은 `done`, `error`, `stopped` 중 하나로 끝납니다. 이벤트별 화면 처리는 [chat-turn.md](features/chat-turn.md#sse-이벤트)에 있습니다.

### 다시 붙기

- 응답 헤더는 `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no` 입니다.
- `GET .../stream` 은 `after` 쿼리와 `Last-Event-ID` 헤더 중 큰 번호의 다음 이벤트부터 보냅니다. 그래서 다시 붙어도 이벤트가 빠지거나 겹치지 않습니다.
- 실행 기록은 실행이 끝나고 60초 동안 남습니다(`runs.RETAIN_SECONDS`). 그 뒤나 서버 재시작 뒤에는 204 입니다.
- 연결이 끊겨도 응답은 백그라운드에서 끝까지 만들어져 저장됩니다.

자세한 규칙은 [streaming-runs.md](features/streaming-runs.md#이벤트-번호와-다시-붙기)에 있습니다.

## 첨부 문서

라우터: `app/api/routers/documents.py` (prefix `/api/documents`). 동작은 [documents.md](features/documents.md)에 있습니다.

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| POST | `/api/documents` | multipart `file` 을 올린다. 201 과 `processing` 상태의 문서 정보. 파싱은 백그라운드에서 한다. 너무 크면 413, 지원하지 않거나 손상된 파일이면 415, 저장소를 쓸 수 없으면 503 | 승인된 사용자 |
| GET | `/api/documents/{document_id}` | 문서 정보와 상태(`processing`, `analyzing`, `indexing`, `ready`, `failed`), 실패 사유, 쪽 수, 글자 수 등 | 승인된 사용자 |
| GET | `/api/documents/{document_id}/file` | 원본을 `Content-Disposition: attachment` 로 내려준다. 보관 기간이 지나 원본이 지워졌으면 410, 저장소를 쓸 수 없으면 503 | 승인된 사용자 |
| DELETE | `/api/documents/{document_id}` | 아직 보내지 않은 첨부 문서를 지운다. 204. 이미 메시지로 보낸 문서나 프로젝트 문서면 409 | 승인된 사용자 |

파일 크기 상한은 `DOCUMENT_MAX_BYTES`(기본 20MB)입니다. 받는 확장자는 `GET /api/info` 의 `documents.extensions` 에 있습니다.

## 프로젝트

라우터: `app/api/routers/projects.py` (prefix `/api/projects`). 동작은 [projects-rag.md](features/projects-rag.md)에 있습니다.

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/projects` | 내 프로젝트 목록, 최근에 바꾼 순 | 승인된 사용자 |
| POST | `/api/projects` | 본문 `{name, instructions?}`. 201 | 승인된 사용자 |
| GET | `/api/projects/{project_id}` | 프로젝트, 문서, 대화, 용량(`used_tokens`, `max_tokens`) | 승인된 사용자 |
| PATCH | `/api/projects/{project_id}` | 본문 `{name?, instructions?}`. 보낸 값만 바꾼다 | 승인된 사용자 |
| DELETE | `/api/projects/{project_id}` | 프로젝트와 그 안의 대화·문서를 지운다. 204. 생성 중인 대화가 있으면 409 | 승인된 사용자 |
| POST | `/api/projects/{project_id}/documents` | multipart `file`. 201 과 `processing` 상태의 문서 정보. 용량이 꽉 찼으면 409, 너무 크면 413, 지원하지 않으면 415, 저장소를 쓸 수 없으면 503 | 승인된 사용자 |
| DELETE | `/api/projects/{project_id}/documents/{document_id}` | 프로젝트 문서를 지운다. 204 | 승인된 사용자 |
| POST | `/api/projects/{project_id}/documents/{document_id}/reprocess` | 저장된 원본으로 처음부터 다시 처리한다. 202. 처리 중이면 409, 원본이 없으면 404, 저장소를 쓸 수 없으면 503 | 승인된 사용자 |
| POST | `/api/projects/{project_id}/reprocess` | 문서를 모두 다시 처리한다. 202 와 다시 시작한 문서 목록. 처리 중이거나 원본이 없는 문서는 건너뛴다 | 승인된 사용자 |
| GET | `/api/projects/{project_id}/documents/{document_id}/view` | 추출한 블록 목록(문서 보기 패널). `?chunk_id=` 를 주면 강조할 위치도 준다 | 승인된 사용자 |
| GET | `/api/projects/{project_id}/images/{element_id}` | 문서 속 그림 원본(출처 카드 미리보기). 저장소를 쓸 수 없으면 503 | 승인된 사용자 |

## 장기 기억

라우터: `app/api/routers/memory.py` (prefix `/api/memory`). 동작은 [memory.md](features/memory.md)에 있습니다.
기억 Store 를 쓸 수 없으면(`ENABLE_MEMORY=false` 등) `GET /api/memory` 는 `available: false` 를 돌려주고, 프로필과 기억을 다루는 나머지 엔드포인트는 503 입니다.

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/memory` | `available`, `enabled`, `profile`, `memories`(최근에 고친 순), 한도(`max_memories`, `max_memory_chars`, `max_profile_chars`) | 승인된 사용자 |
| PUT | `/api/memory/settings` | 본문 `{enabled}`. 내 기억 기능을 켜거나 끈다. 응답 `{enabled}` | 승인된 사용자 |
| PUT | `/api/memory/profile` | 본문 `{text}` (최대 800자). 비우면 프로필을 지운다 | 승인된 사용자 |
| POST | `/api/memory/memories` | 본문 `{content, category?}`. 201. 내용이 잘못됐거나 개수 한도(200개)를 넘으면 422 | 승인된 사용자 |
| PATCH | `/api/memory/memories/{memory_id}` | 본문 `{content?, category?}`. 보낸 값만 바꾼다. 내 기억이 아니면 404 | 승인된 사용자 |
| DELETE | `/api/memory/memories/{memory_id}` | 기억 하나를 지운다. 204. 내 기억이 아니면 404 | 승인된 사용자 |
| DELETE | `/api/memory` | 내 프로필과 기억을 모두 지운다. 204 | 승인된 사용자 |

## 로그인과 가입 신청

라우터: `app/api/routers/auth.py` (prefix `/api/auth`), `app/api/routers/approvals.py` (prefix `/api/auth/approvals`). 동작은 [auth-approval.md](features/auth-approval.md)에 있습니다.

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/auth/me` | `{auth_required, google_login_enabled, user}`. 로그인 전에도 200 이고 `user` 는 `null` | 없음 |
| GET | `/api/auth/google/login` | Google 로그인 화면으로 303 리디렉션. 설정이 없으면 화면으로 돌려보내며 `login_error` 를 붙인다 | 없음 |
| GET | `/api/auth/google/callback` | Google 콜백. 세션 쿠키를 붙여 화면으로 303 리디렉션. 실패하면 `login_error` 를 붙인다 | 없음 |
| POST | `/api/auth/request` | 본문 `{reason}`. 관리자에게 승인·거절 링크 메일을 보낸다. 202 와 `{notified}`. 승인 대기 상태가 아니면 409, 간격(`ACCESS_REQUEST_COOLDOWN_MINUTES`, 기본 10분) 안에 다시 보내면 429 | 로그인 |
| POST | `/api/auth/logout` | 세션과 쿠키를 지운다. 204 | 없음 |
| GET | `/api/auth/approvals/{token}` | 메일 링크의 확인 화면(HTML). 이 요청만으로는 아무것도 바뀌지 않는다. 링크가 만료됐거나 이미 썼으면 410 | 없음 (링크 토큰) |
| POST | `/api/auth/approvals/{token}` | 확인 화면의 버튼. 승인 또는 거절을 처리하고 결과 화면(HTML)을 준다. 링크가 유효하지 않으면 410 | 없음 (링크 토큰) |

## 관리자

라우터: `app/api/routers/admin.py` (prefix `/api/admin`).

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/api/admin/users` | 사용자 목록. 승인 대기가 먼저, 그다음 최근 순. `?status=` 로 거른다. 로컬 사용자는 빠진다 | 관리자 |
| POST | `/api/admin/users/{user_id}/{decision}` | `decision` 은 `approve`, `reject`, `suspend`, `reactivate`. 사용자가 없으면 404, 지금 상태에서 할 수 없으면 409, 관리자 계정을 거절·정지하려 하면 400 | 관리자 |

## 기타

| Method | Path | 설명 | 권한 |
|---|---|---|---|
| GET | `/health` | `{"status": "ok"}` | 없음 |
