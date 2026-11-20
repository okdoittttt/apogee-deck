"""환경 변수 기반 앱 설정."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

GOOGLE_HISTORY_TOKENS = 32000
"""Google provider 의 기본 대화 이력 예산. 넘으면 앞부분을 요약한다 (``CONTEXT_MAX_TOKENS`` 로 바꿀 수 있다)."""

OPENAI_HISTORY_TOKENS = 32000
"""OpenAI provider 의 기본 대화 이력 예산. 매 턴 보내는 입력이 곧 비용이라 제한한다."""


class Settings(BaseSettings):
    """앱 설정. 각 필드는 같은 이름의 대문자 환경 변수(또는 ``.env``)에서 읽는다.

    system 프롬프트는 설정이 아니라 ``app/agents/<에이전트>/prompts/*.md`` 에서 관리한다.

    Attributes:
        llm_provider: 기본 LLM provider. ``anthropic`` | ``google`` | ``ollama`` | ``fake``
            (테스트용 echo 모델). 화면에서 모델을 고르지 않았을 때 쓴다 (``app.llm.catalog`` 참고).
            ``openai`` 도 쓸 수 있다.
        app_timezone: 프롬프트에 넣는 "오늘 날짜" 의 기준 시간대.
        enable_tools: 에이전트 도구 사용 여부. tool calling 을 지원하지 않는 모델이면 끈다.
        enable_vision: 이미지 입력 허용 여부. 모델이 vision 을 지원해야 한다.
        image_token_estimate: 컨텍스트 예산 계산 시 이미지 1장을 몇 토큰으로 볼지.
            프론트엔드가 긴 변 1024px 로 줄여 보내며, 이 크기면 Claude/Qwen 모두 1~1.5K 수준이다.
        anthropic_api_key: Anthropic API 키.
        claude_model: 기본 Claude 모델 ID.
        anthropic_models: 화면에서 고를 수 있는 Claude 모델 (쉼표로 구분). ``claude_model`` 은 항상 포함된다.
        claude_effort: Claude 응답 품질과 비용/지연의 트레이드오프 단계.
        claude_max_tokens: Claude 출력 최대 토큰.
        google_api_key: Google AI Studio(Gemini API) 키.
        google_model: 기본 Gemini API 모델 ID.
        google_models: 화면에서 고를 수 있는 Gemini API 모델 (쉼표로 구분). ``google_model`` 은 항상 포함된다.
        google_max_output_tokens: 출력(추론 포함) 최대 토큰.
        google_include_thoughts: 추론 요약을 받아 화면의 "생각 과정" 에 보여줄지 여부.
        google_temperature: 샘플링 온도. ``None`` 이면 모델 기본값.
        google_timeout_seconds: 요청 한 번의 타임아웃(초).
        google_max_retries: 요청 시도 횟수(첫 요청 포함). 무료 티어는 한도를 넘으면 429 를
            돌려주는데, 많이 재시도하면 응답이 오래 멈춰 있고 한도만 더 쓴다.
        openai_api_key: OpenAI API 키.
        openai_model: 기본 OpenAI 모델 ID.
        openai_models: 화면에서 고를 수 있는 OpenAI 모델 (쉼표로 구분). ``openai_model`` 은 항상 포함된다.
            요금이 비싼 모델이 실수로 쓰이지 않도록 목록을 직접 정한다.
        openai_reasoning_effort: 추론 모델의 추론 강도. ``None`` 이면 추론 설정을 보내지 않는다
            (추론을 지원하지 않는 모델용).
        openai_reasoning_summary: 추론 요약을 받아 화면의 "생각 과정" 에 보여줄지 여부.
            조직 인증이 안 된 계정은 OpenAI 가 거절할 수 있다.
        openai_max_output_tokens: 출력(추론 포함) 최대 토큰.
        openai_timeout_seconds: 요청 한 번의 타임아웃(초).
        openai_max_retries: 실패한 요청을 다시 시도하는 횟수 (첫 요청 제외).
        ollama_base_url: Ollama 서버 주소. macOS 에서는 호스트에서 ``ollama serve`` 를 실행하고
            컨테이너는 ``host.docker.internal`` 로 접근한다.
        ollama_model: Ollama 모델 이름.
        ollama_reasoning: thinking 모델이면 ``True`` 로 두어 출력에서 ``<think>`` 를 분리한다.
            ``None`` 이면 모델 기본 동작.
        ollama_num_ctx: 모델이 한 번에 보는 토큰 수(입력+출력). 클수록 KV 캐시 메모리가 늘어난다.
            넘치면 Ollama 가 앞부분을 조용히 잘라내므로 앱에서 먼저 trim 한다.
        ollama_num_predict: 출력(thinking 포함) 최대 토큰.
        ollama_temperature: 샘플링 온도. ``None`` 이면 모델 권장값(Modelfile)을 쓴다.
        ollama_keep_alive: 마지막 요청 후 모델을 메모리에 유지하는 시간 (예: ``"10m"``).
        ollama_timeout_seconds: Ollama 요청 타임아웃(초). 연결, 응답 대기, 토큰 사이 간격에
            각각 적용된다. 메모리 부족 등으로 Ollama 가 멈췄을 때 요청이 무한히 기다리지 않게 한다.
        fake_stream_delay_seconds: fake 모델이 단어 하나를 내보낼 때마다 기다리는 시간.
            "응답 생성 중" 상황(대기열, 다른 창 거절)을 테스트할 때만 쓴다.
        tavily_api_key: Tavily API 키. 비어 있으면 웹 검색 도구를 등록하지 않는다.
        tavily_search_depth: Tavily 검색 깊이. ``advanced`` 는 관련 본문을 더 잘 뽑지만
            2 크레딧을 쓰고(``basic`` 은 1), 1~2초 더 걸린다.
        web_search_max_results: 웹 검색 한 번에 받을 결과 수.
        web_search_timeout_seconds: 웹 검색 요청 타임아웃(초).
        enable_fetch_url: 웹 페이지 본문 읽기 도구 사용 여부. 키 없이도 동작한다
            (Tavily 키가 있으면 직접 받기가 실패했을 때 Tavily 로 다시 시도한다).
        fetch_max_chars: 페이지 본문을 한 번에 보여줄 최대 글자 수. 긴 페이지는 나눠서 읽는다.
        fetch_timeout_seconds: 페이지 요청 타임아웃(초).
        fetch_max_bytes: 받을 페이지의 최대 크기(바이트).
        compact_tool_results: 이전 턴의 도구 결과를 줄여서 모델에 보낼지 여부. 원본은 상태에
            그대로 남는다 (``agents.context.compact_old_tool_results`` 참고).
        context_max_tokens: 모델에 보낼 대화 이력의 토큰 예산. ``None`` 이면 provider 별 기본값
            (``history_token_budget`` 참고).
        run_stale_seconds: 응답 생성 중 표시가 이 시간 동안 갱신되지 않으면 끝난 것으로 본다.
            정상적으로는 응답이 끝날 때 해제되며, 해제되지 못한 경우를 대비한 안전장치다.
            모델이 토큰 사이에 기다릴 수 있는 시간(``ollama_timeout_seconds``)보다 길어야 한다.
        database_url: 앱 DB URL (SQLAlchemy asyncpg 형식).
        s3_endpoint_url: 첨부 파일 저장소(S3 호환) 주소. Compose 에서는 앱 전용 MinIO
            (``http://minio:9000``). 비어 있으면 첨부 저장소를 쓰지 않는다.
        s3_access_key_id: 저장소 access key.
        s3_secret_access_key: 저장소 secret key.
        s3_bucket: 첨부 파일을 담을 버킷. 없으면 시작할 때 만든다.
        s3_region: 저장소 region. MinIO 는 값을 따지지 않지만 S3 API 서명에 필요하다.
        attachment_retention_days: 첨부 원본을 보관하는 기간(일). 버킷 lifecycle 규칙으로 지우고,
            DB 의 문서 정보와 파싱 결과는 정리 작업이 지운다.
        document_max_bytes: 첨부 문서 하나의 최대 크기(바이트).
        document_max_pages: 첨부 문서 하나의 최대 쪽수. 넘으면 파싱하지 않고 실패로 표시한다.
        document_parse_timeout_seconds: 문서 하나를 파싱하는 최대 시간(초). 넘으면 파싱 프로세스를
            끝내고 실패(시간 초과)로 표시한다.
        document_parse_workers: 동시에 파싱하는 문서 수. OCR 은 CPU 와 메모리를 많이 쓴다.
        document_unsent_ttl_hours: 올렸지만 메시지로 보내지 않은 문서를 지우기까지의 시간.
        document_inline_max_tokens: 이보다 짧은 문서는 본문을 사용자 메시지에 바로 넣는다
            (도구 호출 한 번을 아낀다). 0 이면 항상 ``read_document`` 로 읽게 한다.
        document_read_max_chars: ``read_document`` 가 한 번에 보여줄 최대 글자 수.
        max_documents_per_message: 메시지 하나에 붙일 수 있는 문서 수.
        auth_required: 로그인을 요구할지. ``False`` 면 모든 요청을 로컬 사용자 한 명으로 처리한다
            (로그인 화면이 없는 개발 단계용). **다른 사람이 접속하는 환경에서는 반드시 켠다.**
        google_oauth_client_id: Google 로그인 OAuth 클라이언트 ID.
        google_oauth_client_secret: Google 로그인 OAuth 클라이언트 secret.
        admin_emails: 관리자 Google 이메일 (쉼표로 구분). 처음 로그인할 때 바로 승인되고,
            가입 신청 알림 메일을 받는다.
        frontend_url: 화면 주소. 로그인이 끝나면 여기로 돌려보낸다.
        backend_public_url: 브라우저에서 본 백엔드 주소. Google 로그인 콜백 주소를 만든다
            (``<주소>/api/auth/google/callback`` 을 OAuth 클라이언트에 등록해야 한다).
        session_ttl_days: 로그인 세션 유지 기간(일).
        cookie_secure: 세션 쿠키에 ``Secure`` 를 붙일지. ``None`` 이면 ``frontend_url`` 이 https 일 때만.
        smtp_host: 알림 메일을 보낼 SMTP 서버 (Gmail: ``smtp.gmail.com``).
        smtp_port: SMTP 포트 (587, STARTTLS).
        smtp_username: SMTP 로그인 계정 (보내는 Gmail 주소).
        smtp_password: SMTP 비밀번호. Gmail 은 계정 비밀번호가 아니라 **앱 비밀번호**다.
        mail_from: 보내는 사람 주소. 비우면 ``smtp_username``.
        approval_link_ttl_hours: 가입 승인·거절 링크의 유효 시간.
        access_request_cooldown_minutes: 같은 사람이 가입 신청 메일을 다시 보낼 수 있기까지의 간격.
        langfuse_public_key: Langfuse public key. 비어 있으면 트레이싱이 꺼진다.
        langfuse_secret_key: Langfuse secret key.
        prompt_source: ``file`` 이면 항상 로컬 파일(git 원본), ``langfuse`` 이면 라벨이 붙은
            Langfuse 버전을 쓰고 실패 시 로컬 파일을 쓴다.
        prompt_label: 실행 시 가져올 Langfuse 라벨이자, 동기화 시 새 버전에 붙일 라벨.
        prompt_cache_ttl_seconds: Langfuse 프롬프트 캐시 시간. 만료 후에는 이전 값을 쓰면서
            백그라운드로 갱신한다.
        prompt_sync_on_startup: 백엔드 시작 시 git → Langfuse 프롬프트 자동 동기화 (개발 환경용).
        cors_origins: 허용할 CORS origin (쉼표로 구분).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM ---
    llm_provider: Literal["anthropic", "google", "openai", "ollama", "fake"] = "anthropic"
    app_timezone: str = "Asia/Seoul"
    enable_tools: bool = True
    enable_vision: bool = True
    image_token_estimate: int = 1500

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    anthropic_models: str = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_tokens: int = 64000

    # Google (Gemini API)
    google_api_key: str = ""
    google_model: str = "gemma-4-26b-a4b-it"
    google_models: str = "gemma-4-26b-a4b-it,gemma-4-31b-it"
    google_max_output_tokens: int = 8192
    google_include_thoughts: bool = True
    google_temperature: float | None = None
    google_timeout_seconds: float = 120.0
    google_max_retries: int = 2

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    openai_models: str = "gpt-6-luna,gpt-5-mini,gpt-6.1-terra"
    openai_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = "low"
    openai_reasoning_summary: bool = True
    openai_max_output_tokens: int = 8192
    openai_timeout_seconds: float = 120.0
    openai_max_retries: int = 2

    # Ollama
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_reasoning: bool | None = None
    ollama_num_ctx: int = 32768
    ollama_num_predict: int = 4096
    ollama_temperature: float | None = None
    ollama_keep_alive: str = "10m"
    ollama_timeout_seconds: float = 300.0

    # Fake (테스트용)
    fake_stream_delay_seconds: float = 0.0

    # --- Web search ---
    tavily_api_key: str = ""
    tavily_search_depth: Literal["basic", "advanced"] = "advanced"
    web_search_max_results: int = 5
    web_search_timeout_seconds: float = 20.0
    enable_fetch_url: bool = True
    fetch_max_chars: int = 8000
    fetch_timeout_seconds: float = 15.0
    fetch_max_bytes: int = 5_000_000

    # --- Context management ---
    compact_tool_results: bool = True
    context_max_tokens: int | None = None

    # --- Concurrency ---
    run_stale_seconds: float = 600.0

    # --- Database ---
    database_url: str = "postgresql+asyncpg://agent:agent@postgres:5432/agent"

    # --- Attachment storage (S3 호환) ---
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "attachments"
    s3_region: str = "us-east-1"
    attachment_retention_days: int = 30

    # --- Documents ---
    document_max_bytes: int = 20 * 1024 * 1024
    document_max_pages: int = 100
    document_parse_timeout_seconds: float = 180.0
    document_parse_workers: int = 1
    document_unsent_ttl_hours: int = 24
    document_inline_max_tokens: int = 2000
    document_read_max_chars: int = 8000
    max_documents_per_message: int = 5

    # --- Auth ---
    auth_required: bool = True
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    admin_emails: str = ""
    frontend_url: str = "http://localhost:3000"
    backend_public_url: str = "http://localhost:8000"
    session_ttl_days: int = 30
    cookie_secure: bool | None = None

    # --- Mail (가입 신청 알림) ---
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    approval_link_ttl_hours: int = 48
    access_request_cooldown_minutes: int = 10

    # --- Tracing (Langfuse) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # --- Prompt management ---
    prompt_source: Literal["file", "langfuse"] = "file"
    prompt_label: str = "production"
    prompt_cache_ttl_seconds: int = 60
    prompt_sync_on_startup: bool = False

    # --- CORS ---
    cors_origins: str = "http://localhost:3000"

    @property
    def history_token_budget(self) -> int | None:
        """모델에 보낼 대화 이력의 토큰 예산.

        ``context_max_tokens`` 가 있으면 그 값을 쓴다. 없으면 Ollama 는
        ``num_ctx - num_predict - 256``, Google 은 ``GOOGLE_HISTORY_TOKENS``,
        OpenAI 는 ``OPENAI_HISTORY_TOKENS`` 이고,
        Anthropic 은 trim 하지 않는다(``None``).
        Claude 는 컨텍스트가 크고, 이력을 자르면 프롬프트 캐시와 thinking 연속성이 깨진다.
        Google 은 컨텍스트는 크지만 무료 티어의 분당 토큰 한도가 있어서 매 턴 보내는 양을 제한한다.
        """
        if self.context_max_tokens is not None:
            return self.context_max_tokens
        if self.llm_provider == "ollama":
            return self.ollama_num_ctx - self.ollama_num_predict - 256
        if self.llm_provider == "google":
            return GOOGLE_HISTORY_TOKENS
        if self.llm_provider == "openai":
            return OPENAI_HISTORY_TOKENS
        return None

    @property
    def checkpointer_db_url(self) -> str:
        """LangGraph Postgres checkpointer 용 DB URL (psycopg 형식)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def admin_email_set(self) -> set[str]:
        """관리자 이메일 (소문자)."""
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def google_login_enabled(self) -> bool:
        """Google 로그인 클라이언트가 설정되어 있는지."""
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)

    @property
    def mail_enabled(self) -> bool:
        """알림 메일을 보낼 수 있는지 (SMTP 계정과 앱 비밀번호가 설정됨)."""
        return bool(self.smtp_host and self.smtp_username and self.smtp_password)

    @property
    def session_cookie_secure(self) -> bool:
        """세션 쿠키에 ``Secure`` 를 붙일지."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.frontend_url.startswith("https://")

    @property
    def storage_enabled(self) -> bool:
        """첨부 저장소 주소와 키가 모두 설정되어 쓸 수 있는지 여부."""
        return bool(self.s3_endpoint_url and self.s3_access_key_id and self.s3_secret_access_key)

    @property
    def tracing_enabled(self) -> bool:
        """Langfuse 키가 모두 설정되어 트레이싱을 쓸 수 있는지 여부."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def cors_origin_list(self) -> list[str]:
        """``cors_origins`` 를 리스트로 나눈 값."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 반환한다. 프로세스당 한 번만 읽고 캐시한다.

    Returns:
        환경 변수와 ``.env`` 를 읽어 만든 ``Settings``.
    """
    return Settings()
