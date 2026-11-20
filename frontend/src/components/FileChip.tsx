"use client";

import { CloseIcon, FileIcon, SpinnerIcon, WarningIcon } from "./icons";

type ChipStatus = "uploading" | "processing" | "analyzing" | "indexing" | "ready" | "failed";

const STATUS_TEXT: Record<ChipStatus, string> = {
  uploading: "올리는 중",
  processing: "읽는 중",
  analyzing: "그림 분석 중",
  indexing: "검색 준비 중",
  ready: "",
  failed: "읽지 못함",
};

/**
 * 첨부 문서 하나를 보여주는 칩.
 * 입력창에서는 상태(올리는 중/읽는 중/실패)와 빼기 버튼을, 보낸 메시지에서는 원본 받기 링크를 보여준다.
 */
export function FileChip({
  name,
  meta,
  status = "ready",
  error,
  href,
  onRemove,
  removeLabel = "첨부 빼기",
}: {
  name: string;
  meta?: string;
  status?: ChipStatus;
  error?: string | null;
  href?: string; // 원본 받기 주소
  onRemove?: () => void;
  removeLabel?: string; // 빼기 버튼의 접근성 문구 (예: "첨부 빼기", "문서 삭제")
}) {
  const failed = status === "failed";
  const busy = status === "uploading" || status === "processing" ||
    status === "analyzing" ||
    status === "indexing";
  const sub = failed ? (error ?? STATUS_TEXT.failed) : busy ? STATUS_TEXT[status] : meta;

  const body = (
    <>
      <span
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
          failed ? "bg-red-500/15 text-red-600 dark:text-red-300" : "bg-hover text-muted"
        }`}
      >
        {busy ? (
          <SpinnerIcon className="h-4 w-4" />
        ) : failed ? (
          <WarningIcon />
        ) : (
          <FileIcon />
        )}
      </span>
      <span className="min-w-0 text-left">
        <span className="block truncate text-[13px] font-medium text-fg">{name}</span>
        {sub && (
          <span
            className={`block truncate text-[11px] ${failed ? "text-red-600 dark:text-red-300" : "text-muted"}`}
          >
            {sub}
          </span>
        )}
      </span>
    </>
  );

  return (
    <div
      className={`group relative flex w-60 max-w-full items-center rounded-xl border bg-surface ${
        failed ? "border-red-500/40" : "border-line"
      }`}
      title={failed && error ? `${name}\n${error}` : name}
    >
      {href ? (
        <a
          href={href}
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl px-2.5 py-2 hover:bg-hover"
          aria-label={`${name} 원본 받기`}
        >
          {body}
        </a>
      ) : (
        <div className="flex min-w-0 flex-1 items-center gap-2.5 px-2.5 py-2">{body}</div>
      )}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-sm hover:text-fg"
          aria-label={`${name} ${removeLabel}`}
          title={removeLabel}
        >
          <CloseIcon />
        </button>
      )}
    </div>
  );
}
