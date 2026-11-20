"""글자 수로 토큰 수를 어림하는 기준값.

대화 이력 예산(``app.orchestration.context``)과 첨부 문서 길이(``app.documents.reading``)가 같은 기준을 쓴다.
"""

# ASCII 가 아닌 글자(한글 등)는 ASCII 보다 토큰을 훨씬 많이 쓴다. qwen3.5 로 실측하면
# 한국어 문장은 글자당 약 0.61 토큰이었다(ASCII 는 글자당 약 0.25). 넘치지 않도록 조금 크게 잡는다.
NON_ASCII_CHARS_PER_TOKEN = 1.5
ASCII_CHARS_PER_TOKEN = 4.0
