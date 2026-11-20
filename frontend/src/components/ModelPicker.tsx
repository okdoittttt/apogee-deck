"use client";

import { useEffect, useRef, useState } from "react";
import type { ModelOption } from "@/lib/api";
import { CheckIcon, ChevronIcon, SpinnerIcon } from "./icons";

interface ModelPickerProps {
  models: ModelOption[];
  value: string | null;
  onChange: (id: string) => void;
  /** 열 때마다 목록을 새로 받는다 (그사이 Ollama 를 켰거나 껐을 수 있다). */
  onOpen?: () => void;
  loading?: boolean;
}

/** provider 별로 묶는다 (서버가 준 순서 유지). */
function groupByProvider(models: ModelOption[]): [string, ModelOption[]][] {
  const groups = new Map<string, ModelOption[]>();
  for (const m of models) {
    const list = groups.get(m.provider_label) ?? [];
    list.push(m);
    groups.set(m.provider_label, list);
  }
  return [...groups.entries()];
}

/**
 * 입력창의 모델 선택 드롭다운. 고른 모델은 다음에 보내는 메시지부터 쓴다.
 * 입력창이 화면 아래에 있으므로 목록은 위로 연다.
 */
export function ModelPicker({ models, value, onChange, onOpen, loading }: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = models.find((m) => m.id === value);

  // 바깥을 누르거나 Esc 를 누르면 닫는다.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function toggle() {
    if (!open) onOpen?.();
    setOpen((v) => !v);
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="모델 선택"
        className="flex h-8 max-w-[220px] items-center gap-1 rounded-lg px-2 text-xs text-muted hover:bg-hover hover:text-fg"
      >
        <span className="truncate">{selected?.label ?? (value ? value : "모델 없음")}</span>
        <ChevronIcon
          className={`h-3 w-3 shrink-0 transition-transform ${open ? "-rotate-90" : "rotate-90"}`}
        />
      </button>

      {open && (
        <div
          role="listbox"
          aria-label="모델"
          className="absolute bottom-full right-0 z-30 mb-2 max-h-80 w-64 overflow-y-auto rounded-xl border border-line bg-surface p-1 shadow-[0_8px_24px_rgba(0,0,0,0.12)]"
        >
          {models.length === 0 && (
            <div className="px-3 py-2.5 text-xs leading-relaxed text-muted">
              {loading ? (
                <span className="flex items-center gap-2">
                  <SpinnerIcon /> 모델 목록을 불러오는 중…
                </span>
              ) : (
                <>
                  사용 가능한 모델이 없습니다.
                  <br />
                  .env 에 API 키를 넣거나 Ollama 를 실행해 주세요.
                </>
              )}
            </div>
          )}
          {groupByProvider(models).map(([provider, items]) => (
            <div key={provider} className="py-0.5">
              <div className="px-3 pb-1 pt-1.5 text-[11px] font-medium text-subtle">{provider}</div>
              {items.map((m) => {
                const active = m.id === value;
                return (
                  <button
                    key={m.id}
                    type="button"
                    role="option"
                    aria-selected={active}
                    onClick={() => {
                      onChange(m.id);
                      setOpen(false);
                    }}
                    className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-hover ${
                      active ? "text-fg" : "text-muted"
                    }`}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{m.label}</span>
                      {m.label !== m.model && (
                        <span className="block truncate font-mono text-[11px] text-subtle">
                          {m.model}
                        </span>
                      )}
                    </span>
                    {active && <CheckIcon className="h-3.5 w-3.5 shrink-0" />}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
