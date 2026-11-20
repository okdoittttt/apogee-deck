"""키워드 검색용 토큰 만들기 (한국어는 두 글자 조각).

Postgres 기본 전문 검색(``simple`` 설정)은 띄어쓰기로만 단어를 나눠서 ``"부산의"`` 와 ``"부산"`` 을 다른 단어로
본다. 한국어 형태소 분석기 없이 조사·어미가 붙은 말을 찾으려고, 한글·한자 낱말은 낱말 자체와 **두 글자 조각**
(``"부산의"`` → ``부산의``, ``부산``, ``산의``)으로 나눈다. 영문·숫자는 소문자 낱말 그대로 쓴다.

여기서 만든 토큰을 공백으로 이어 ``to_tsvector('simple', ...)`` 에 넣고, 검색어 토큰은 ``|`` (또는)로 이어
``to_tsquery('simple', ...)`` 로 찾는다. 토큰은 한글·한자·영문·숫자만 남기므로 tsquery 문법 글자가 섞이지 않는다.
"""

import re

_WORD = re.compile(r"[0-9a-z]+|[가-힣㐀-鿿]+")
"""영문·숫자 낱말, 또는 한글(완성형)·한자 낱말."""

MAX_QUERY_TOKENS = 64
"""검색어 토큰 상한 (긴 질문을 그대로 넣어도 tsquery 가 너무 커지지 않게)."""


def keyword_tokens(text: str) -> list[str]:
    """글자에서 키워드 토큰을 뽑는다 (순서 유지, 중복 포함).

    Args:
        text: 본문이나 검색어.

    Returns:
        토큰 목록. 한글·한자 낱말은 낱말과 두 글자 조각, 영문·숫자는 소문자 낱말.
    """
    tokens: list[str] = []
    for word in _WORD.findall(text.lower()):
        tokens.append(word)
        if len(word) > 2 and not word[0].isascii():
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


def keyword_document(text: str) -> str:
    """``to_tsvector('simple', ...)`` 에 넣을 글자 (토큰을 공백으로 이은 것).

    Args:
        text: 청크의 임베딩 입력 (문서 이름·제목 경로 포함).

    Returns:
        토큰을 공백으로 이은 글자.
    """
    return " ".join(keyword_tokens(text))


def keyword_query(text: str) -> str | None:
    """검색어를 ``to_tsquery('simple', ...)`` 에 넣을 글자로 만든다 (토큰 중 하나라도 있으면 맞음).

    Args:
        text: 검색어.

    Returns:
        ``"부산 | 매출 | 85"`` 같은 글자. 토큰이 없으면 ``None``.
    """
    unique = list(dict.fromkeys(keyword_tokens(text)))[:MAX_QUERY_TOKENS]
    return " | ".join(unique) or None
