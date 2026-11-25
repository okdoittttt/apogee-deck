"use client";

import { documentFileUrl, type Step, type ToolDocument } from "@/lib/api";
import { formatBytes, kindLabel } from "@/lib/documents";
import { useOpenEdited } from "./EditedDocumentPanel";
import { FileChip } from "./FileChip";

/** 응답의 도구 단계에서 에이전트가 만든 문서(수정본, 새 문서)를 모은다. 같은 문서는 한 번만 넣는다. */
export function collectDocuments(steps: Step[]): ToolDocument[] {
  const seen = new Set<string>();
  const documents: ToolDocument[] = [];
  for (const step of steps) {
    if (step.type !== "tool") continue;
    for (const doc of step.documents ?? []) {
      if (seen.has(doc.document_id)) continue;
      seen.add(doc.document_id);
      documents.push(doc);
    }
  }
  return documents;
}

/** 칩의 보조 설명: "수정본 v2 · 텍스트 · 1KB", 새로 만든 문서는 "새 문서 · CSV · 1KB". */
function editedMeta(doc: ToolDocument): string {
  // 직전 문서가 없으면 에이전트가 새로 만든 문서다 (수정본은 항상 직전 문서가 있다).
  const isNew = doc.source_document_id == null;
  const parts = [isNew ? "새 문서" : doc.revision ? `수정본 v${doc.revision}` : "수정본"];
  if (doc.kind) parts.push(kindLabel(doc.kind));
  if (doc.size_bytes != null) parts.push(formatBytes(doc.size_bytes));
  return parts.join(" · ");
}

/**
 * 에이전트가 이번 답변에서 만든 수정본·새 문서를 답변 아래에 파일 칩으로 보여준다.
 * 누르면 오른쪽 패널(미리보기·변경이력·버전)을 열고, 패널이 없는 화면에서는 바로 받는다.
 */
export function EditedFiles({ steps }: { steps: Step[] }) {
  const openEdited = useOpenEdited();
  const documents = collectDocuments(steps);
  if (documents.length === 0) return null;
  return (
    <section className="mt-3 flex flex-wrap gap-2" aria-label="만든 문서">
      {documents.map((doc) => (
        <FileChip
          key={doc.document_id}
          name={doc.filename}
          meta={editedMeta(doc)}
          href={documentFileUrl(doc.document_id)}
          onOpen={
            openEdited
              ? () => openEdited({ documentId: doc.document_id, filename: doc.filename, kind: doc.kind })
              : undefined
          }
        />
      ))}
    </section>
  );
}
