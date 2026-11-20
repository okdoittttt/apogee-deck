import type { DocumentInfo } from "./api";

/** 입력창에 붙인 문서. 올리는 중 → 서버에서 읽는 중 → 준비됨/실패 순서로 바뀐다. */
export interface PendingDocument {
  localId: string;
  filename: string;
  size: number;
  status: "uploading" | "processing" | "analyzing" | "ready" | "failed";
  error?: string | null;
  doc?: DocumentInfo; // 서버가 받은 뒤의 정보 (id, 쪽 수 등)
}

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function isImageFile(file: File): boolean {
  return file.type.startsWith("image/");
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

const KIND_LABELS: Record<string, string> = {
  pdf: "PDF",
  docx: "Word",
  xlsx: "Excel",
  pptx: "PowerPoint",
  hwp: "한글",
  hwpml: "한글",
  hwpx: "한글",
  txt: "텍스트",
  md: "Markdown",
  csv: "CSV",
};

/** 파일 칩에 보여줄 형식 이름 (서버가 확인한 형식, 없으면 확장자). */
export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind.toUpperCase();
}

/** 파일 칩의 보조 설명: "PDF · 3쪽 · 1.2MB". */
export function documentMeta(doc: Pick<DocumentInfo, "kind" | "page_count" | "size_bytes">): string {
  const parts = [kindLabel(doc.kind)];
  if (doc.page_count) parts.push(`${doc.page_count}쪽`);
  parts.push(formatBytes(doc.size_bytes));
  return parts.join(" · ");
}
