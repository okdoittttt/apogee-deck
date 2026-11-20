"use client";

import type { Attachment } from "@/lib/images";
import { ClockIcon, CloseIcon, InfoIcon } from "./icons";

export interface QueuedMessage {
  id: string;
  text: string;
  attachments: Attachment[];
  model: string | null; // 입력할 때 고른 모델 (나중에 보내져도 이 모델로 보낸다)
}

/** 응답이 끝나면 보낼 대기 메시지. 오류로 자동 전송이 멈추면 직접 보내기 버튼을 보여준다. */
export function QueuedMessageBubble({
  message,
  paused,
  onSendNow,
  onRemove,
}: {
  message: QueuedMessage;
  paused: boolean;
  onSendNow: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="mt-7 flex flex-col items-end gap-1.5">
      <div className="flex max-w-[85%] items-start gap-2 rounded-2xl border border-dashed border-line-strong px-4 py-2.5 opacity-70">
        <div className="min-w-0">
          {message.attachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {message.attachments.map((a) => (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  key={a.id}
                  src={a.previewUrl}
                  alt="대기 중인 첨부 이미지"
                  className="h-12 w-12 rounded-md object-cover"
                />
              ))}
            </div>
          )}
          {message.text && (
            <p className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">
              {message.text}
            </p>
          )}
        </div>
        <button
          onClick={onRemove}
          className="mt-1 shrink-0 text-muted hover:text-fg"
          aria-label="대기 메시지 삭제"
          title="대기 메시지 삭제"
        >
          <CloseIcon />
        </button>
      </div>
      <div className="flex items-center gap-2 text-xs text-muted">
        <ClockIcon />
        {paused ? (
          <>
            <span>오류로 자동 전송을 멈췄습니다</span>
            <button
              onClick={onSendNow}
              className="rounded-md border border-line px-2 py-0.5 text-fg hover:bg-hover"
            >
              대기 메시지 보내기
            </button>
          </>
        ) : (
          <span>대기 중 · 응답이 끝나면 보냅니다</span>
        )}
      </div>
    </div>
  );
}

/** 다른 창에서 이 대화의 응답을 생성 중일 때 보여주는 안내. */
export function RemoteRunningNotice() {
  return (
    <div className="mt-7 flex items-center gap-3 rounded-xl border border-line px-4 py-3 text-sm text-muted">
      <InfoIcon className="h-4 w-4 shrink-0" />
      <span>
        다른 창에서 이 대화의 응답을 생성하고 있습니다. 완료되면 여기에 표시됩니다.
      </span>
    </div>
  );
}
