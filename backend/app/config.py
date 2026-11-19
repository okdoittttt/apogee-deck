"""환경 변수 기반 앱 설정."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """앱 설정. 각 필드는 같은 이름의 대문자 환경 변수(또는 ``.env``)에서 읽는다.

    system 프롬프트는 설정이 아니라 ``app/agents/<에이전트>/prompts/*.md`` 에서 관리한다.

    Attributes:
        llm_provider: 사용할 LLM. ``anthropic`` | ``ollama`` | ``fake`` (테스트용 echo 모델).
        app_timezone: 프롬프트에 넣는 "오늘 날짜" 의 기준 시간대.
        enable_tools: 에이전트 도구 사용 여부. tool calling 을 지원하지 않는 모델이면 끈다.
        enable_vision: 이미지 입력 허용 여부. 모델이 vision 을 지원해야 한다.
        image_token_estimate: 컨텍스트 예산 계산 시 이미지 1장을 몇 토큰으로 볼지.
            프론트엔드가 긴 변 1024px 로 줄여 보내며, 이 크기면 Claude/Qwen 모두 1~1.5K 수준이다.
        anthropic_api_key: Anthropic API 키.
        claude_model: Claude 모델 ID.
        claude_effort: Claude 응답 품질과 비용/지연의 트레이드오프 단계.
        claude_max_tokens: Claude 출력 최대 토큰.
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
        compact_tool_results: 이전 턴의 도구 결과를 줄여서 모델에 보낼지 여부. 원본은 상태에
            그대로 남는다 (``agents.context.compact_old_tool_results`` 참고).
        context_max_tokens: 모델에 보낼 대화 이력의 토큰 예산. ``None`` 이면 provider 별 기본값
            (``history_token_budget`` 참고).
        run_stale_seconds: 응답 생성 중 표시가 이 시간 동안 갱신되지 않으면 끝난 것으로 본다.
            정상적으로는 응답이 끝날 때 해제되며, 해제되지 못한 경우를 대비한 안전장치다.
            모델이 토큰 사이에 기다릴 수 있는 시간(``ollama_timeout_seconds``)보다 길어야 한다.
        database_url: 앱 DB URL (SQLAlchemy asyncpg 형식).
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
    llm_provider: Literal["anthropic", "ollama", "fake"] = "anthropic"
    app_timezone: str = "Asia/Seoul"
    enable_tools: bool = True
    enable_vision: bool = True
    image_token_estimate: int = 1500

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_tokens: int = 64000

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

    # --- Context management ---
    compact_tool_results: bool = True
    context_max_tokens: int | None = None

    # --- Concurrency ---
    run_stale_seconds: float = 600.0

    # --- Database ---
    database_url: str = "postgresql+asyncpg://agent:agent@postgres:5432/agent"

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
        ``num_ctx - num_predict - 256`` 이고, Anthropic 은 trim 하지 않는다(``None``).
        Claude 는 컨텍스트가 크고, 이력을 자르면 프롬프트 캐시와 thinking 연속성이 깨진다.
        """
        if self.context_max_tokens is not None:
            return self.context_max_tokens
        if self.llm_provider == "ollama":
            return self.ollama_num_ctx - self.ollama_num_predict - 256
        return None

    @property
    def checkpointer_db_url(self) -> str:
        """LangGraph Postgres checkpointer 용 DB URL (psycopg 형식)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

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
