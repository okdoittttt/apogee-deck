"use client";

import { useEffect } from "react";
import { CloseIcon, WarningIcon } from "./icons";

const AUTO_HIDE_MS = 6000;

/** 화면 오른쪽 아래에 잠시 떠 있는 경고 알림. 몇 초 뒤 자동으로 사라진다. */
export function Toast({ message, onClose }: { message: string | null; onClose: () => void }) {
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(onClose, AUTO_HIDE_MS);
    return () => clearTimeout(timer);
  }, [message, onClose]);

  if (!message) return null;
  return (
    <div
      role="status"
      className="fixed bottom-5 right-5 z-50 flex max-w-sm items-start gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-sm text-fg shadow-lg"
    >
      <WarningIcon className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
      <span className="flex-1 leading-relaxed">{message}</span>
      <button onClick={onClose} className="mt-0.5 text-muted hover:text-fg" aria-label="알림 닫기">
        <CloseIcon />
      </button>
    </div>
  );
}
