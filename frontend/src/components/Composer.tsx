"use client";

import { useEffect, useRef } from "react";
import { type PendingDocument, documentMeta } from "@/lib/documents";
import { type Attachment, MAX_IMAGES } from "@/lib/images";
import { FileChip } from "./FileChip";
import { ArrowUpIcon, CloseIcon, PlusIcon, StopIcon } from "./icons";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  attachments: Attachment[];
  onAddFiles: (files: Iterable<File>) => void;
  onRemoveAttachment: (id: string) => void;
  /** 입력창에 붙인 문서 (올리는 중 / 읽는 중 / 준비됨 / 실패) */
  documents: PendingDocument[];
  onRemoveDocument: (localId: string) => void;
  /** 문서 첨부를 쓸 수 있는지 (서버에 첨부 저장소가 설정됨) */
  documentsEnabled: boolean;
  /** 파일 선택 창에 보여줄 문서 확장자 (점 없이) */
  documentExtensions: string[];
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  /** 대기열이 찼는지 (응답 생성 중에는 메시지 1개까지만 대기열에 넣을 수 있다) */
  queueFull: boolean;
  /** 입력창 오른쪽 아래에 넣을 모델 선택 드롭다운 */
  modelPicker?: React.ReactNode;
  visionEnabled: boolean;
  autoFocus?: boolean;
  placeholder?: string;
}

const MAX_TEXTAREA_HEIGHT = 240;

export function Composer({
  value,
  onChange,
  attachments,
  onAddFiles,
  onRemoveAttachment,
  documents,
  onRemoveDocument,
  documentsEnabled,
  documentExtensions,
  onSend,
  onStop,
  streaming,
  queueFull,
  modelPicker,
  visionEnabled,
  autoFocus,
  placeholder = "무엇이든 물어보세요",
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const hasContent = value.trim().length > 0 || attachments.length > 0 || documents.length > 0;
  // 문서를 다 읽기 전이거나 읽지 못한 문서가 있으면 보낼 수 없다.
  const documentsBusy = documents.some((d) => d.status === "uploading" || d.status === "processing");
  const documentsFailed = documents.some((d) => d.status === "failed");
  // 응답 생성 중에도 대기열이 비어 있으면 보낼 수 있다 (응답이 끝나면 전송됨).
  const canSend = hasContent && !documentsBusy && !documentsFailed && !(streaming && queueFull);
  const canAttach = visionEnabled || documentsEnabled;
  const accept = [
    ...(visionEnabled ? ["image/png", "image/jpeg", "image/webp", "image/gif"] : []),
    ...(documentsEnabled ? documentExtensions.map((ext) => `.${ext}`) : []),
  ].join(",");
  const sendTitle = documentsBusy
    ? "문서를 다 읽은 뒤에 보낼 수 있습니다"
    : documentsFailed
      ? "읽지 못한 문서를 빼고 보내 주세요"
      : streaming
        ? "응답이 끝나면 보내기 (1개까지)"
        : "전송 (Enter)";

  // 입력 내용에 맞춰 높이 자동 조절
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [value]);

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  return (
    <div className="rounded-2xl border border-line bg-surface shadow-[0_1px_2px_rgba(0,0,0,0.04),0_4px_16px_rgba(0,0,0,0.04)] transition-colors focus-within:border-line-strong">
      {documents.length > 0 && (
        <div className="flex flex-wrap gap-2 px-3 pt-3">
          {documents.map((d) => (
            <FileChip
              key={d.localId}
              name={d.filename}
              meta={d.doc ? documentMeta(d.doc) : undefined}
              status={d.status}
              error={d.error}
              onRemove={() => onRemoveDocument(d.localId)}
            />
          ))}
        </div>
      )}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 px-3 pt-3">
          {attachments.map((a) => (
            <div key={a.id} className="group relative">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={a.previewUrl}
                alt="첨부 미리보기"
                className="h-16 w-16 rounded-lg border border-line object-cover"
              />
              <button
                type="button"
                onClick={() => onRemoveAttachment(a.id)}
                className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-sm hover:text-fg"
                aria-label="첨부 삭제"
              >
                <CloseIcon />
              </button>
            </div>
          ))}
        </div>
      )}

      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onPaste={(e) => {
          const files = Array.from(e.clipboardData.files);
          const usable = files.some((f) =>
            f.type.startsWith("image/") ? visionEnabled : documentsEnabled,
          );
          if (usable) {
            e.preventDefault();
            onAddFiles(files);
          }
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            // 대기열이 찼을 때도 부모에게 넘겨서 안내를 띄우게 한다.
            if (hasContent && !documentsBusy && !documentsFailed) onSend();
          }
        }}
        rows={1}
        placeholder={placeholder}
        className="block max-h-60 w-full resize-none bg-transparent px-4 pb-2 pt-3.5 text-[15px] leading-relaxed text-fg placeholder:text-subtle focus:outline-none"
      />

      <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5">
        <div className="flex items-center gap-1">
          <input
            ref={fileInputRef}
            type="file"
            accept={accept}
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) onAddFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={!canAttach || (streaming && queueFull)}
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted hover:bg-hover hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="파일 첨부"
            title={
              canAttach
                ? [
                    visionEnabled && `이미지 최대 ${MAX_IMAGES}장`,
                    documentsEnabled && "문서(PDF, Word, Excel, PowerPoint, 한글, 텍스트)",
                  ]
                    .filter(Boolean)
                    .join(" · ") + " · 붙여넣기/드래그 가능"
                : "파일을 첨부할 수 없습니다"
            }
          >
            <PlusIcon className="h-4 w-4" />
          </button>
        </div>

        <div className="flex items-center gap-2">
          {modelPicker}
          {streaming && (
            <button
              type="button"
              onClick={onStop}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-fg hover:bg-hover"
              aria-label="응답 중지"
              title="중지 (대기 메시지가 있으면 바로 보냄)"
            >
              <StopIcon />
            </button>
          )}
          {(!streaming || hasContent) && (
            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              className="flex h-8 w-8 items-center justify-center rounded-lg bg-fg text-app hover:opacity-85 disabled:opacity-25"
              aria-label={streaming ? "대기열에 추가" : "전송"}
              title={sendTitle}
            >
              <ArrowUpIcon />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
