# 실행 환경 설정

> 처음 실행할 때부터 LLM, 로그인, 선택 기능을 켜기까지의 설정 순서입니다.
> 관련 파일: `.env.example`, `docker-compose.yml`, `docker-compose.langfuse.yml`, `Makefile`

설정값의 의미와 기본값은 `.env.example` 의 주석에 있습니다. 이 문서는 무엇을 언제 채워야 하는지만 다룹니다.
각 기능의 세부 설정은 기능 문서의 표를 보세요.

## 1. 처음 실행

```bash
cp .env.example .env    # 아래 2~3절을 보고 채움
make up                 # 전체 실행 (바뀐 이미지는 다시 빌드)
```

| 서비스 | 주소 | 로그인 |
|---|---|---|
| 채팅 화면 | http://localhost:3000 | Google 로그인 (3절) |
| 백엔드 API 문서 | http://localhost:8000/docs | |
| Langfuse | http://localhost:3100 | `.env` 의 `LANGFUSE_INIT_USER_EMAIL` / `PASSWORD` |
| MinIO 콘솔 | http://localhost:9101 | `.env` 의 `MINIO_ROOT_USER` / `PASSWORD` |

- 포트가 이미 쓰이고 있으면 `.env` 의 `*_PORT` 값을 바꿉니다.
- `docker compose` 대신 `make` 를 쓰세요. `make` 는 현재 커밋 해시를 `GIT_COMMIT` 으로 넘기고(커밋하지 않은 변경이 있으면 `-dirty`),
  이 값이 Langfuse 프롬프트 버전에 "어떤 코드에서 나온 버전인지"로 기록됩니다. `docker compose` 를 직접 쓰면 비어 있습니다.
- **Langfuse 없이** 앱만 띄우려면 `.env` 의 `COMPOSE_FILE` 줄을 주석 처리하고 `LANGFUSE_*_KEY` 를 비웁니다. 트레이싱과 피드백 점수가 꺼지고, 나머지는 그대로 동작합니다.
- 문서 OCR 모델(약 30MB)은 이미지를 만들 때 미리 받으므로 따로 받을 필요가 없습니다.

## 2. LLM provider

하나 이상 채우면 됩니다. 키가 있는 provider 의 모델만 화면의 모델 목록에 나옵니다.
`LLM_PROVIDER` 와 `*_MODEL` 은 처음 선택될 기본 모델입니다. 목록과 모델 ID 형식은 [llm-providers.md](../features/llm-providers.md)에 있습니다.

| provider | 채울 값 | 참고 |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `OPENAI_API_KEY` | 선불 크레딧에서 차감됩니다. 목록에는 `OPENAI_MODELS` 에 적은 모델만 나옵니다. 조직 인증이 안 된 계정이면 `OPENAI_REASONING_SUMMARY=false` |
| `google` | `GOOGLE_API_KEY` ([AI Studio](https://aistudio.google.com) 에서 발급) | 무료 티어는 분당·하루 요청 한도가 낮고, 입력이 Google 제품 개선에 쓰일 수 있습니다. 개인정보나 회사 자료는 넣지 마세요 |
| `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | 아래 참고 |
| `fake` | `LLM_PROVIDER=fake` | LLM 없이 받은 메시지를 돌려줍니다. 파이프라인 확인과 테스트용 |

**OpenAI 키는 다른 provider 로 채팅해도 필요할 수 있습니다.** 프로젝트 문서의 임베딩과 그림 설명이 OpenAI 를 씁니다.
키가 없으면 프로젝트 검색은 키워드 검색만 쓰고, 그림은 OCR 글자로 대신합니다([projects-rag.md](../features/projects-rag.md)).

### Ollama

macOS 에서는 Ollama 를 Docker 가 아니라 **호스트에서** 실행합니다. Docker 안에서는 Metal GPU 를 쓸 수 없기 때문입니다.

```bash
brew install ollama && ollama serve
ollama pull qwen3.5:9b     # .env: LLM_PROVIDER=ollama, OLLAMA_MODEL=qwen3.5:9b, OLLAMA_REASONING=true
ollama show qwen3.5:9b     # 지원 기능(tools, thinking, vision)과 최대 context 확인
ollama ps                  # 실제로 올라간 context 크기와 메모리 확인
```

- 도구 호출을 지원하지 않는 모델이면 `ENABLE_TOOLS=false`, 이미지를 지원하지 않는 모델이면 `ENABLE_VISION=false` 로 둡니다.
- `OLLAMA_NUM_CTX` 가 클수록 메모리를 많이 씁니다. 대화 이력 예산은 이 값에서 계산합니다([context-management.md](../features/context-management.md)).
- Ollama 가 꺼져 있으면 Ollama 모델만 목록에서 빠지고 앱은 그대로 동작합니다.

## 3. 로그인 (Google OAuth + 승인제)

Google 계정으로 로그인하고, 관리자가 승인한 사람만 쓸 수 있습니다. 동작은 [auth-approval.md](../features/auth-approval.md)에 있습니다.

1. Google Cloud Console 에서 OAuth 클라이언트(웹 애플리케이션)를 만들고, 승인된 리디렉션 URI 에
   `http://localhost:8000/api/auth/google/callback` 을 넣습니다. 배포할 때는 도메인 주소로 바꿉니다.
2. `.env` 에 `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` 을 넣습니다.
3. `ADMIN_EMAILS` 에 관리자 이메일을 넣습니다. 이 주소로 로그인하면 바로 승인되고 `/admin` 화면을 쓸 수 있습니다.
4. 승인 요청 메일은 Gmail SMTP 로 보냅니다. `SMTP_USERNAME` 에 보내는 Gmail 주소를, `SMTP_PASSWORD` 에 **앱 비밀번호**를 넣습니다.
   계정 비밀번호가 아닙니다. Google 계정의 2단계 인증을 켠 뒤 앱 비밀번호를 만들 수 있습니다.

로그인 없이 혼자 개발할 때는 `AUTH_REQUIRED=false` 로 둡니다. 로컬 사용자 한 명으로 동작합니다.
**다른 사람이 접속할 수 있는 환경에서는 반드시 `true` 로 둡니다.**

## 4. 선택 기능

| 기능 | 켜는 방법 | 문서 |
|---|---|---|
| 웹 검색 | `TAVILY_API_KEY` ([app.tavily.com](https://app.tavily.com), 무료 플랜 월 1,000 크레딧) | [web-fetch.md](../features/web-fetch.md) |
| 웹 페이지 읽기 | 기본으로 켜짐 (키 불필요). 끄려면 `ENABLE_FETCH_URL=false` | [web-fetch.md](../features/web-fetch.md) |
| 장기 기억 | 기본으로 켜짐. 끄려면 `ENABLE_MEMORY=false` | [memory.md](../features/memory.md) |
| 도구 결정 (d3) | `ollama pull d3:2b` 후 `ENABLE_TOOL_DECISION=true` | [tool-decision.md](../features/tool-decision.md) |

- 웹 검색어는 외부 서비스(Tavily)로 전송됩니다. 민감한 대화를 할 때는 `TAVILY_API_KEY` 를 비워 검색을 끄세요.

## 5. 다른 기기에서 접속하기

모든 포트는 기본적으로 `127.0.0.1` 에만 열려서 이 컴퓨터에서만 접속할 수 있습니다.

다른 기기에서 접속하게 하려면 다음을 바꿉니다.

| 설정 | 값 |
|---|---|
| `BIND_HOST` | `0.0.0.0` (같은 네트워크의 누구나 포트에 닿을 수 있게 됨) |
| `AUTH_REQUIRED` | `true` (꼭 확인) |
| `NEXT_PUBLIC_API_URL` | 브라우저가 백엔드를 부를 주소. 브라우저가 API 를 직접 부르기 때문에 필요합니다 |
| `CORS_ORIGINS`, `FRONTEND_URL`, `BACKEND_PUBLIC_URL` | 화면과 백엔드의 공개 주소 |
| OAuth 리디렉션 URI | 3절의 콜백 주소를 공개 주소로 바꿈 |

https 로 열면 세션 쿠키에 `Secure` 가 붙습니다.
