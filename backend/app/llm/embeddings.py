"""프로젝트 문서 검색용 임베딩.

``EMBEDDING_PROVIDER`` 로 고른다.

- ``openai``: OpenAI 임베딩 모델(``EMBEDDING_MODEL``, 기본 ``text-embedding-3-small``). ``OPENAI_API_KEY`` 필요.
- ``fake``: 글자 조각(1·2글자)을 해시해서 만드는 벡터. 네트워크 없이 같은 글자가 많을수록 가깝게 나온다.
  테스트와 키 없는 개발용이다.

청크마다 만든 모델 이름(``model_id``)을 저장해서, 모델을 바꾸면 예전 청크를 다시 임베딩할 수 있게 한다.
"""

import hashlib
import math

from langchain_core.embeddings import Embeddings

from app.core.config import Settings


class EmbeddingUnavailableError(Exception):
    """임베딩을 쓸 수 없다 (OpenAI 키가 없음)."""


def model_id(settings: Settings) -> str:
    """청크에 기록할 임베딩 모델 이름. 이 값이 다르면 다시 임베딩한다.

    Args:
        settings: 앱 설정.

    Returns:
        ``"<provider>:<모델>:<차원>"`` (예: ``"openai:text-embedding-3-small:1536"``).
    """
    model = settings.embedding_model if settings.embedding_provider == "openai" else "hashing"
    return f"{settings.embedding_provider}:{model}:{settings.embedding_dimensions}"


def build_embeddings(settings: Settings) -> Embeddings:
    """설정에 맞는 임베딩 모델을 만든다.

    Args:
        settings: 앱 설정 (provider, 모델, 차원, 키, 시간 제한).

    Returns:
        LangChain 임베딩 모델.

    Raises:
        EmbeddingUnavailableError: OpenAI 를 골랐는데 키가 없을 때.
    """
    if settings.embedding_provider == "fake":
        return HashingEmbeddings(settings.embedding_dimensions)
    if not settings.openai_api_key:
        raise EmbeddingUnavailableError("OPENAI_API_KEY 가 없어 문서 검색을 쓸 수 없습니다.")
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        api_key=settings.openai_api_key,
        chunk_size=settings.embedding_batch_size,
        timeout=settings.embedding_timeout_seconds,
        max_retries=2,
        # 청크는 입력 한도(8192 토큰)보다 훨씬 작다. 켜 두면 tiktoken 이 토크나이저 파일을 내려받는다.
        check_embedding_ctx_length=False,
    )


class HashingEmbeddings(Embeddings):
    """글자 조각 해시 임베딩 (``fake``). 같은 글자 조각을 많이 나눠 가진 글끼리 코사인 유사도가 높다.

    Attributes:
        dimensions: 벡터 차원.
    """

    def __init__(self, dimensions: int) -> None:
        """임베딩 모델을 만든다.

        Args:
            dimensions: 벡터 차원.
        """
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        compact = "".join(text.lower().split())
        grams = list(compact) + [compact[i : i + 2] for i in range(len(compact) - 1)]
        for gram in grams:
            # hash() 는 프로세스마다 달라서 쓰지 않는다.
            digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """여러 글을 임베딩한다.

        Args:
            texts: 글 목록.

        Returns:
            벡터 목록.
        """
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """검색어를 임베딩한다.

        Args:
            text: 검색어.

        Returns:
            벡터.
        """
        return self._embed(text)
