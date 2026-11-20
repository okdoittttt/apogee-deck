/** macOS 계열(⌘ 키를 쓰는 기기)인지. 서버 렌더링 중에는 false. */
export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
}

/** 단축키 표시 문자열. 예) mac: "⌘N", "⇧⌘O" / 그 밖: "Ctrl+N", "Ctrl+Shift+O" */
export function shortcutLabel(key: string, { shift = false } = {}): string {
  if (isMacPlatform()) return `${shift ? "⇧" : ""}⌘${key}`;
  return `Ctrl+${shift ? "Shift+" : ""}${key}`;
}
