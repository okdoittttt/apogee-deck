"""채팅에 첨부한 이미지: 원본은 첨부 저장소(MinIO)에, 대화 상태에는 참조만 둔다.

이미지를 base64 로 LangGraph checkpoint 에 넣으면, checkpoint 가 턴마다 대화 상태를 새로 저장해서
같은 이미지가 계속 쌓인다. 그래서 checkpoint 의 사용자 메시지에는 이미지 ID 만 담은 블록을 넣고,
모델을 부르기 직전에(``resolve_images``) 저장소에서 원본을 읽어 base64 블록으로 바꾼다.

이미지는 대화 내용 그 자체라서 문서 원본과 달리 보관 기간으로 지우지 않는다. 대화를 지울 때 함께 지운다.
"""

import asyncio
import base64
import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from app.core.storage import ObjectNotFoundError, ObjectStorage
from app.models import MessageImage

logger = logging.getLogger(__name__)

IMAGE_REF_KEY = "image_id"
"""참조 블록에서 이미지 ID 를 담는 ``extras`` 키."""

MISSING_IMAGE_TEXT = "[An image was attached here but is no longer available.]"
"""원본을 찾지 못한 이미지 자리에 모델에게 보여줄 글."""


def storage_key(image_id: uuid.UUID) -> str:
    """이미지 원본의 객체 키.

    Args:
        image_id: 이미지 ID.

    Returns:
        ``"images/<이미지 ID>"``. 문서(``documents/``)와 접두어가 달라서 보관 기간 규칙을 받지 않는다.
    """
    return f"images/{image_id}"


async def store_images(
    storage: ObjectStorage, images: Sequence[tuple[str, bytes]]
) -> list[MessageImage]:
    """이미지 원본을 저장소에 올리고 DB 에 넣을 행을 만든다.

    하나라도 실패하면 이미 올린 것을 지우고 예외를 다시 던진다.

    Args:
        storage: 첨부 저장소.
        images: ``(mime_type, 원본 바이트)`` 목록. 순서가 메시지 안의 순서다.

    Returns:
        ``message_id`` 만 비어 있는 ``MessageImage`` 목록 (메시지에 붙여서 저장한다).
    """
    rows = [
        MessageImage(
            id=(image_id := uuid.uuid4()),
            position=i,
            mime_type=mime,
            storage_key=storage_key(image_id),
        )
        for i, (mime, _) in enumerate(images)
    ]
    try:
        await asyncio.gather(
            *(
                storage.put(row.storage_key, data, row.mime_type)
                for row, (_, data) in zip(rows, images, strict=True)
            )
        )
    except BaseException:
        await delete_images(storage, [row.storage_key for row in rows])
        raise
    return rows


async def delete_images(storage: ObjectStorage | None, keys: Iterable[str]) -> None:
    """이미지 원본을 지운다. 실패해도 기록만 남긴다.

    Args:
        storage: 첨부 저장소. ``None`` 이면 아무것도 하지 않는다.
        keys: 지울 객체 키.
    """
    if storage is None:
        return
    for key in keys:
        try:
            await storage.delete(key)
        except Exception:
            logger.warning("failed to delete image object %s", key, exc_info=True)


def image_ref_block(image: MessageImage) -> dict[str, Any]:
    """Checkpoint 에 넣을 이미지 참조 블록.

    LangChain 표준 이미지 블록 모양(``type="image"``)이라 토큰 추정과 요약은 이미지로 센다.
    원본 데이터(``base64``/``url``)가 없으므로 모델에 보내기 전에 ``resolve_images`` 로 바꿔야 한다.

    Args:
        image: 저장한 이미지 행.

    Returns:
        ``{"type": "image", "mime_type": ..., "extras": {"image_id": ...}}``.
    """
    return {"type": "image", "mime_type": image.mime_type, "extras": {IMAGE_REF_KEY: str(image.id)}}


def _ref_id(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get("type") != "image":
        return None
    if block.get("base64") or block.get("url") or block.get("data"):
        return None  # 원본이 이미 들어 있다 (이전 방식으로 저장된 대화)
    extras = block.get("extras")
    return extras.get(IMAGE_REF_KEY) if isinstance(extras, dict) else None


async def resolve_images(
    storage: ObjectStorage | None, messages: list[BaseMessage]
) -> list[BaseMessage]:
    """이미지 참조 블록을 저장소의 원본(base64)으로 바꾼 메시지 목록을 돌려준다.

    원본 메시지(checkpoint 상태)는 바꾸지 않고 바뀐 메시지만 복사한다. 원본을 찾지 못한 이미지는
    안내 글로 바꿔서, 지워진 이미지 하나 때문에 대화 전체가 실패하지 않게 한다.

    Args:
        storage: 첨부 저장소. ``None`` 이면 모든 참조를 안내 글로 바꾼다.
        messages: 모델에 보낼 메시지.

    Returns:
        모델에 보낼 수 있는 메시지 목록.
    """
    ids = {
        ref
        for m in messages
        if isinstance(m.content, list)
        for b in m.content
        if (ref := _ref_id(b))
    }
    if not ids:
        return messages
    loaded = dict(zip(ids, await asyncio.gather(*(_load(storage, i) for i in ids)), strict=True))

    resolved: list[BaseMessage] = []
    for m in messages:
        if not isinstance(m.content, list) or not any(_ref_id(b) for b in m.content):
            resolved.append(m)
            continue
        content: list[Any] = []
        for b in m.content:
            ref = _ref_id(b)
            if ref is None:
                content.append(b)
            elif (data := loaded[ref]) is None:
                content.append({"type": "text", "text": MISSING_IMAGE_TEXT})
            else:
                content.append({"type": "image", "base64": data, "mime_type": b["mime_type"]})
        resolved.append(m.model_copy(update={"content": content}))
    return resolved


async def _load(storage: ObjectStorage | None, image_id: str) -> str | None:
    if storage is None:
        return None
    try:
        raw = await storage.get(storage_key(uuid.UUID(image_id)))
    except ObjectNotFoundError:
        logger.warning("image %s not found in storage", image_id)
        return None
    return base64.b64encode(raw).decode("ascii")
