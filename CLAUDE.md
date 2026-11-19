# CLAUDE.md

LangGraph 기반 LLM 에이전트 웹앱 (개인 학습 프로젝트). 구조와 실행 방법은 `README.md` 참고.

## Git 규칙 (반드시 지킬 것)

- **push 하지 않는다.** push 는 항상 사용자가 직접 한다. 요청받아도 push 명령을 실행하지 말고 명령어만 안내한다.
- **commit 은 사용자가 명시적으로 요청할 때만** 한다. 작업이 끝나도 먼저 변경 내역을 보여주고 요청을 기다린다.
- **commit 메시지에 Claude 트레일러를 넣지 않는다.** `Co-Authored-By: Claude …`, `🤖 Generated with Claude Code` 등 Claude/AI 관련 문구를 메시지 어디에도 넣지 않는다.
- 이 저장소는 **개인 GitHub 계정 전용**이다. 회사 계정과 섞이면 안 된다.
  - remote 는 SSH 별칭 `git@github.com-okdoittttt:okdoittttt/okdoitttt.git` 만 사용한다 (HTTPS 주소, 다른 별칭 금지).
  - 작성자는 저장소 로컬 설정 `okdoittttt <94339333+okdoittttt@users.noreply.github.com>` 이다. 전역 git 설정은 건드리지 않는다.
- public 저장소다. commit 전에 비밀값(API 키 등)이 포함되지 않았는지 확인한다. `.env` 는 커밋하지 않는다.

## 코드 스타일

### 백엔드 (Python): Google Style Docstring

모든 모듈, 공개 클래스, 공개 함수/메서드에 Google Style Docstring 을 쓴다.

- 설명은 한국어로 쓰고, 섹션 이름(`Args`, `Returns`, `Yields`, `Raises`, `Attributes`, `Example`)은 영어 그대로 쓴다.
- 첫 줄은 한 줄 요약, 필요하면 빈 줄 뒤에 자세한 설명.
- 타입은 타입 힌트로 표현하고 docstring 에는 반복하지 않는다.
- 인자/반환값이 없거나 자명한 섹션은 생략한다.
- `ruff` 의 docstring 규칙(`D`, `convention = "google"`)으로 검사한다. `make lint` 가 통과해야 한다.
  ruff 는 docstring 누락과 형식만 잡으므로, `Args`/`Returns`/`Raises` 내용은 직접 챙긴다.
- **예외: 에이전트 도구(`@tool`)의 docstring 은 영어로 쓴다.** LLM 에게 도구 설명으로 전달되기 때문이다.
  `@tool(parse_docstring=True)` 로 선언해서 `Args` 설명이 도구 인자 스키마에 들어가게 한다.
- 테스트 함수는 docstring 을 강제하지 않는다 (쓰면 Google Style 로).

```python
def render(name: str, /, **variables: Any) -> Prompt:
    """이름으로 프롬프트를 렌더링한다.

    Langfuse 를 쓰도록 설정되어 있으면 라벨이 붙은 버전을 쓰고,
    가져올 수 없으면 로컬 파일로 렌더링한다.

    Args:
        name: 프롬프트 이름 (예: ``"chat/system"``).
        **variables: 템플릿 변수. frontmatter 의 ``variables`` 와 정확히 일치해야 한다.

    Returns:
        렌더링된 프롬프트와 출처/버전 정보.

    Raises:
        PromptError: 이름이 잘못됐거나 변수가 빠지거나 남을 때.
    """
```

### 공통
- 주석과 문서는 한국어로 쓴다.
- 백엔드는 `ruff` 규칙을 따른다 (`make lint`).

## 작업 방식

- 검증: `make test` (LLM 없이 fake 모델로 실행), `make lint`, 스키마 변경 시 `make db-check`.
- LLM 이 필요 없는 확인은 `LLM_PROVIDER=fake` 로 한다. 로컬 Ollama 모델은 노트북 메모리를 많이 쓴다.
- DB 스키마 변경은 `app/models.py` 수정 → `make db-revision m="..."` → 생성된 파일 검토.
- 프롬프트는 `app/agents/<에이전트>/prompts/*.md` 와 `app/prompts/shared/` 에서 관리한다. 코드에서는 `prompts.render("<에이전트>/<이름>", ...)` 로 이름으로 부른다.
