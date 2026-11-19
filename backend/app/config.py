from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM provider ---
    llm_provider: Literal["anthropic", "ollama", "fake"] = "anthropic"
    # system 프롬프트는 app/agents/<에이전트>/prompts/*.md 에서 관리한다.
    app_timezone: str = "Asia/Seoul"  # 프롬프트의 오늘 날짜 기준
    enable_tools: bool = True
    enable_vision: bool = True  # 이미지 입력 허용 (모델이 vision 을 지원해야 함)
    # 컨텍스트 예산 계산 시 이미지 1장을 몇 토큰으로 볼지 (1024px 기준 Claude/Qwen 모두 1~1.5K 수준)
    image_token_estimate: int = 1500

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    claude_max_tokens: int = 64000

    # Ollama (macOS: 호스트에서 `ollama serve` 실행 → 컨테이너는 host.docker.internal 로 접근)
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_reasoning: bool | None = None  # thinking 모델이면 true (출력에서 <think> 분리)
    # 모델이 한 번에 보는 토큰 수(입력+출력). 클수록 KV 캐시 메모리가 늘어난다.
    # 넘치면 Ollama 가 앞부분을 조용히 잘라내므로, 앱에서 먼저 trim 한다(context_max_tokens).
    ollama_num_ctx: int = 8192
    ollama_num_predict: int = 4096  # 출력(thinking 포함) 최대 토큰
    ollama_temperature: float | None = None  # None 이면 모델 권장값(Modelfile) 사용
    ollama_keep_alive: str = "10m"  # 마지막 요청 후 모델을 메모리에 유지하는 시간

    # --- Context management ---
    # 모델에 보낼 대화 이력의 토큰 예산. 비우면 provider 별 기본값:
    #   ollama    → num_ctx - num_predict - 여유분
    #   anthropic → trim 안 함 (1M 컨텍스트. 이력을 자르면 캐시/thinking 연속성이 깨짐)
    context_max_tokens: int | None = None

    @property
    def history_token_budget(self) -> int | None:
        if self.context_max_tokens is not None:
            return self.context_max_tokens
        if self.llm_provider == "ollama":
            return self.ollama_num_ctx - self.ollama_num_predict - 256
        return None

    # --- Database ---
    database_url: str = "postgresql+asyncpg://agent:agent@postgres:5432/agent"

    @property
    def checkpointer_db_url(self) -> str:
        # LangGraph Postgres checkpointer 는 psycopg(3) 형식 URL 을 사용
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    # --- Tracing (Langfuse). 키가 비어 있으면 트레이싱 비활성화 ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    # --- Prompt management ---
    # file: 항상 로컬 파일(git 원본) / langfuse: Langfuse 에서 라벨이 붙은 버전 사용, 실패 시 로컬 파일
    prompt_source: Literal["file", "langfuse"] = "file"
    prompt_label: str = "production"  # 실행 시 가져올 라벨 = sync 시 새 버전에 붙일 라벨
    prompt_cache_ttl_seconds: int = (
        60  # Langfuse 프롬프트 캐시 (만료 후엔 이전 값 쓰며 백그라운드 갱신)
    )
    prompt_sync_on_startup: bool = False  # 개발 환경: 백엔드 시작 시 git → Langfuse 자동 동기화

    # --- CORS (comma-separated origins) ---
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
