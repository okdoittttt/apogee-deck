"""도구 결과의 출처 자리 표시.

출처가 있는 도구(웹 검색, 페이지 읽기, 프로젝트 문서 검색)는 결과 글에 ``source_marker(i)`` 를 넣는다.
``i`` 는 그 결과의 artifact ``sources`` 안의 순서(1부터)다. 도구 노드가 끝나면
``app.orchestration.citations`` 가 한 턴 안에서 이어지는 번호(``[9]`` 등)로 바꾼다.
도구가 동시에 실행돼도 번호가 겹치지 않도록, 번호는 도구가 아니라 한 곳에서 매긴다.
"""

import re

SOURCE_MARKER = re.compile(r"⟦source:(\d+)⟧")


def source_marker(index: int) -> str:
    """Artifact ``sources`` 의 ``index`` 번째(1부터) 출처를 가리키는 자리 표시.

    Args:
        index: 출처 순서 (1부터).

    Returns:
        ``⟦source:3⟧`` 같은 글자. 도구 노드가 끝나면 ``[번호]`` 로 바뀐다.
    """
    return f"⟦source:{index}⟧"
