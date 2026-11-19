"use client";

import { useEffect, useRef } from "react";
import { type Attachment, MAX_IMAGES } from "@/lib/images";
import { ArrowUpIcon, CloseIcon, ImageIcon, PlusIcon, StopIcon } from "./icons";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  attachments: Attachment[];
  onAddFiles: (files: Iterable<File>) => void;
  onRemoveAttachment: (id: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  modelLabel?: string;
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
  onSend,
  onStop,
  streaming,
  modelLabel,
  visionEnabled,
  autoFocus,
  placeholder = "무엇이든 물어보세요",
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const canSend = !streaming && (value.trim().length > 0 || attachments.length > 0);

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
          if (visionEnabled && files.some((f) => f.type.startsWith("image/"))) {
            e.preventDefault();
            onAddFiles(files);
          }
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            if (canSend) onSend();
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
            accept="image/png,image/jpeg,image/webp,image/gif"
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
            disabled={!visionEnabled || streaming || attachments.length >= MAX_IMAGES}
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted hover:bg-hover hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="이미지 첨부"
            title={
              visionEnabled
                ? `이미지 첨부 (최대 ${MAX_IMAGES}장 · 붙여넣기/드래그 가능)`
                : "현재 모델은 이미지를 지원하지 않습니다"
            }
          >
            <PlusIcon className="h-4 w-4" />
          </button>
          {attachments.length > 0 && (
            <span className="flex items-center gap-1 px-1.5 text-xs text-muted">
              <ImageIcon className="h-3.5 w-3.5" />
              {attachments.length}/{MAX_IMAGES}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {modelLabel && (
            <span className="max-w-[180px] truncate text-xs text-muted" title={modelLabel}>
              {modelLabel}
            </span>
          )}
          {streaming ? (
            <button
              type="button"
              onClick={onStop}
              className="flex h-8 w-8 items-center justify-center rounded-lg bg-fg text-app hover:opacity-85"
              aria-label="응답 중지"
              title="중지"
            >
              <StopIcon />
            </button>
          ) : (
            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              className="flex h-8 w-8 items-center justify-center rounded-lg bg-fg text-app hover:opacity-85 disabled:opacity-25"
              aria-label="전송"
              title="전송 (Enter)"
            >
              <ArrowUpIcon />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
