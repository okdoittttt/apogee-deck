"""화면에서 고를 수 있는 모델 목록.

모델은 ``"<provider>:<모델 이름>"`` 형식의 ID 로 구분한다 (예: ``google:gemma-4-26b-a4b-it``,
``ollama:qwen3.5:9b``). provider 는 첫 번째 ``:`` 앞부분이다.

목록에는 지금 바로 쓸 수 있는 모델만 넣는다.

- Anthropic / Google / OpenAI: API 키가 있으면 설정의 모델 목록
- Ollama: 서버가 응답하면 설치된 모델 (꺼져 있으면 빠진다)
- fake: 기본 provider 가 ``fake`` 일 때만 (테스트용)
"""

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.llm import model_name

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "google": "Google",
    "openai": "OpenAI",
    "ollama": "Ollama (로컬)",
    "fake": "테스트",
}

MODEL_LABELS = {
    "gemma-4-26b-a4b-it": "Gemma 4 26B A4B",
    "gemma-4-31b-it": "Gemma 4 31B",
}
"""모델 이름 → 화면 표시 이름. 없으면 모델 이름을 그대로 보여준다."""

_MODEL_FIELDS = {
    "anthropic": "claude_model",
    "google": "google_model",
    "openai": "openai_model",
    "ollama": "ollama_model",
}
"""provider → 모델 이름을 담는 설정 필드."""

OLLAMA_LIST_TIMEOUT_SECONDS = 1.5
OLLAMA_LIST_CACHE_SECONDS = 15.0
_ollama_cache: tuple[float, str, list[str]] | None = None


class UnknownModelError(ValueError):
    """형식이 잘못됐거나 지금 쓸 수 없는 모델 ID."""


@dataclass(frozen=True)
class ModelOption:
    """화면에 보여줄 모델 하나.

    Attributes:
        provider: ``anthropic`` | ``google`` | ``openai`` | ``ollama`` | ``fake``.
        model: provider 안에서의 모델 이름.
    """

    provider: str
    model: str

    @property
    def id(self) -> str:
        """``"<provider>:<모델 이름>"`` 형식의 ID."""
        return f"{self.provider}:{self.model}"

    @property
    def label(self) -> str:
        """화면 표시 이름."""
        return MODEL_LABELS.get(self.model, self.model)

    @property
    def provider_label(self) -> str:
        """화면 표시용 provider 이름."""
        return PROVIDER_LABELS.get(self.provider, self.provider)


def model_id(settings: Settings) -> str:
    """설정이 가리키는 모델의 ID.

    Args:
        settings: 앱 설정.

    Returns:
        ``"<provider>:<모델 이름>"``. fake 는 ``"fake:fake"``.
    """
    return f"{settings.llm_provider}:{model_name(settings)}"


def parse_model_id(value: str) -> tuple[str, str]:
    """모델 ID 를 provider 와 모델 이름으로 나눈다.

    Args:
        value: ``"<provider>:<모델 이름>"``.

    Returns:
        ``(provider, 모델 이름)``.

    Raises:
        UnknownModelError: 형식이 잘못됐거나 알 수 없는 provider 일 때.
    """
    provider, sep, name = value.partition(":")
    if not sep or not name or provider not in PROVIDER_LABELS:
        raise UnknownModelError(value)
    return provider, name


def settings_for(settings: Settings, value: str) -> Settings:
    """모델 ID 에 맞게 provider 와 모델 이름만 바꾼 설정을 만든다.

    Args:
        settings: 기본 설정.
        value: 모델 ID.

    Returns:
        ``llm_provider`` 와 해당 provider 의 모델 필드를 바꾼 복사본.

    Raises:
        UnknownModelError: 모델 ID 형식이 잘못됐을 때.
    """
    provider, name = parse_model_id(value)
    update: dict[str, str] = {"llm_provider": provider}
    if field := _MODEL_FIELDS.get(provider):
        update[field] = name
    return settings.model_copy(update=update)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _with_default(models: list[str], default: str) -> list[str]:
    """기본 모델이 목록에 없으면 맨 앞에 넣는다."""
    return models if default in models else [default, *models]


async def _ollama_models(settings: Settings) -> list[str]:
    """Ollama 에 설치된 모델 이름. 서버가 응답하지 않으면 빈 목록.

    모델 선택 창을 열 때마다 부르므로 잠깐 캐시한다. Ollama 를 꺼 둔 경우가 많아서
    타임아웃을 짧게 둔다.
    """
    global _ollama_cache
    now = time.monotonic()
    if _ollama_cache and _ollama_cache[1] == settings.ollama_base_url:
        cached_at, _, names = _ollama_cache
        if now - cached_at < OLLAMA_LIST_CACHE_SECONDS:
            return names
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_LIST_TIMEOUT_SECONDS) as client:
            res = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            res.raise_for_status()
            names = sorted(m["name"] for m in res.json().get("models", []))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.debug("ollama is not reachable; hiding its models", exc_info=True)
        names = []
    _ollama_cache = (now, settings.ollama_base_url, names)
    return names


async def available_models(settings: Settings) -> list[ModelOption]:
    """지금 쓸 수 있는 모델 목록.

    Args:
        settings: 앱 설정.

    Returns:
        provider 순서(OpenAI, Google, Anthropic, Ollama, fake)로 정렬한 모델 목록.
    """
    options: list[ModelOption] = []
    if settings.openai_api_key:
        names = _with_default(_split(settings.openai_models), settings.openai_model)
        options += [ModelOption("openai", n) for n in names]
    if settings.google_api_key:
        names = _with_default(_split(settings.google_models), settings.google_model)
        options += [ModelOption("google", n) for n in names]
    if settings.anthropic_api_key:
        names = _with_default(_split(settings.anthropic_models), settings.claude_model)
        options += [ModelOption("anthropic", n) for n in names]
    options += [ModelOption("ollama", n) for n in await _ollama_models(settings)]
    if settings.llm_provider == "fake":
        options.append(ModelOption("fake", "fake"))
    return options


def default_model_id(settings: Settings, options: list[ModelOption]) -> str | None:
    """처음 선택될 모델의 ID.

    ``.env`` 의 ``LLM_PROVIDER`` 와 해당 모델이 목록에 있으면 그 모델, 없으면 목록의 첫 모델이다.

    Args:
        settings: 앱 설정.
        options: ``available_models`` 의 결과.

    Returns:
        모델 ID. 쓸 수 있는 모델이 없으면 ``None``.
    """
    ids = [o.id for o in options]
    configured = model_id(settings)
    if configured in ids:
        return configured
    return ids[0] if ids else None
