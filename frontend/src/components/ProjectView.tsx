"use client";

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  type Conversation,
  type DocumentInfo,
  type Project,
  type ProjectDetail,
  createProject,
  deleteProject,
  deleteProjectDocument,
  documentFileUrl,
  getProject,
  reprocessProjectDocument,
  reprocessProjectDocuments,
  updateProject,
  uploadProjectDocument,
} from "@/lib/api";
import { documentMeta } from "@/lib/documents";
import { FileChip } from "./FileChip";
import { FolderIcon, UploadIcon } from "./icons";

const POLL_MS = 1000;

/** 문서 칩의 보조 설명: "PDF · 3쪽 · 1.2MB · 그림 4개 · 약 4,200 토큰". */
function projectDocumentMeta(doc: DocumentInfo): string {
  const parts = [documentMeta(doc)];
  if (doc.image_count) parts.push(`그림 ${doc.image_count}개`);
  if (doc.token_count) parts.push(`약 ${doc.token_count.toLocaleString()} 토큰`);
  return parts.join(" · ");
}

/** 설명을 만들지 못한 그림이 있으면 알려준다 (그 그림은 그림 속 글자만 참고한다). */
function fallbackNote(doc: DocumentInfo): string | null {
  const n = doc.image_fallback_count ?? 0;
  return doc.status === "ready" && n > 0 ? `그림 ${n}개는 설명을 만들지 못해 그림 속 글자만 참고합니다.` : null;
}

/** 새 프로젝트 만들기: 이름과 지시문을 받는다. */
export function NewProjectForm({
  onCreated,
  onCancel,
}: {
  onCreated: (project: Project) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      onCreated(await createProject({ name: name.trim(), instructions }));
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-2xl px-4 pt-10">
      <h2 className="mb-6 font-display text-[28px] text-fg">새 프로젝트</h2>
      <label className="mb-1.5 block text-sm font-medium text-fg">이름</label>
      <input
        autoFocus
        value={name}
        maxLength={100}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && void submit()}
        placeholder="예: 2024 사업 보고서 분석"
        className="mb-5 w-full rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[15px] text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none"
      />
      <label className="mb-1.5 block text-sm font-medium text-fg">
        지시문 <span className="font-normal text-muted">(선택)</span>
      </label>
      <textarea
        value={instructions}
        maxLength={20000}
        onChange={(e) => setInstructions(e.target.value)}
        rows={5}
        placeholder="이 프로젝트의 모든 대화에서 지킬 내용을 적어 주세요. 예: 답변은 표로 정리하고 근거 문서를 밝혀 줘."
        className="w-full resize-y rounded-xl border border-line bg-surface px-3.5 py-2.5 text-sm leading-relaxed text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none"
      />
      {error && <p className="mt-3 text-sm text-red-600 dark:text-red-300">{error}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <button onClick={onCancel} className="rounded-lg px-3.5 py-2 text-sm text-muted hover:bg-hover hover:text-fg">
          취소
        </button>
        <button
          onClick={() => void submit()}
          disabled={!name.trim() || saving}
          className="rounded-lg bg-fg px-3.5 py-2 text-sm font-medium text-app disabled:opacity-40"
        >
          만들기
        </button>
      </div>
    </div>
  );
}

/**
 * 프로젝트 화면: 새 대화 입력창, 대화 목록, 문서(올리기·지우기·용량), 지시문, 이름 바꾸기, 삭제.
 * 입력창(composer)은 바깥에서 받는다. 보내면 바깥에서 이 프로젝트 안에 새 대화를 만든다.
 * 끌어다 놓은 파일은 registerDrop 으로 받은 함수로 이 프로젝트에 올린다.
 */
export function ProjectView({
  projectId,
  composer,
  documentExtensions,
  onOpenConversation,
  onChanged,
  onDeleted,
  onToast,
  registerDrop,
}: {
  projectId: string;
  composer: ReactNode;
  documentExtensions: string[];
  onOpenConversation: (id: string) => void;
  /** 이름이 바뀌는 등 사이드바 목록을 다시 읽어야 할 때 */
  onChanged: () => void;
  onDeleted: () => void;
  onToast: (message: string) => void;
  registerDrop: (handler: ((files: FileList) => void) | null) => void;
}) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [savingInstructions, setSavingInstructions] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const d = await getProject(projectId);
      setDetail(d);
      setLoadError(null);
      return d;
    } catch (e) {
      setLoadError((e as Error).message);
      return null;
    }
  }, [projectId]);

  // 프로젝트를 열 때 읽고, 입력칸을 서버 값으로 맞춘다.
  useEffect(() => {
    setDetail(null);
    void load().then((d) => {
      if (!d) return;
      setName(d.name);
      setInstructions(d.instructions);
    });
  }, [load]);

  // 읽는 중이거나 그림을 분석 중인 문서가 있으면 끝날 때까지 다시 읽는다.
  const processing =
    detail?.documents.some((d) => d.status === "processing" || d.status === "analyzing" || d.status === "indexing") ?? false;
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [processing, load]);

  const upload = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files);
      if (!list.length) return;
      setUploading((n) => n + list.length);
      for (const file of list) {
        try {
          await uploadProjectDocument(projectId, file);
        } catch (e) {
          onToast(`${file.name}: ${(e as Error).message}`);
        } finally {
          setUploading((n) => n - 1);
        }
      }
      await load();
    },
    [projectId, load, onToast],
  );

  useEffect(() => {
    registerDrop((files) => void upload(files));
    return () => registerDrop(null);
  }, [registerDrop, upload]);

  async function saveName() {
    const next = name.trim();
    if (!detail || !next || next === detail.name) {
      setName(detail?.name ?? "");
      return;
    }
    try {
      await updateProject(projectId, { name: next });
      await load();
      onChanged();
    } catch (e) {
      onToast((e as Error).message);
      setName(detail.name);
    }
  }

  async function saveInstructions() {
    setSavingInstructions(true);
    try {
      await updateProject(projectId, { instructions });
      await load();
      onToast("지시문을 저장했습니다. 다음 응답부터 적용됩니다.");
    } catch (e) {
      onToast((e as Error).message);
    } finally {
      setSavingInstructions(false);
    }
  }

  async function removeDocument(doc: DocumentInfo) {
    if (!window.confirm(`"${doc.filename}" 문서를 프로젝트에서 삭제할까요?`)) return;
    try {
      await deleteProjectDocument(projectId, doc.id);
      await load();
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  async function reprocessDocument(doc: DocumentInfo) {
    try {
      await reprocessProjectDocument(projectId, doc.id);
      await load();
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  async function reprocessAll() {
    if (!detail) return;
    const ok = window.confirm(
      `문서 ${detail.documents.length}개를 저장된 원본으로 다시 처리할까요?\n그림 설명을 다시 만들어서 시간이 걸리고 비용이 듭니다.`,
    );
    if (!ok) return;
    try {
      const started = await reprocessProjectDocuments(projectId);
      onToast(`문서 ${started.length}개를 다시 처리합니다.`);
      await load();
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  async function remove() {
    if (!detail) return;
    const ok = window.confirm(
      `"${detail.name}" 프로젝트를 삭제할까요?\n프로젝트 안의 대화 ${detail.conversations.length}개와 문서 ${detail.documents.length}개가 모두 삭제되며 되돌릴 수 없습니다.`,
    );
    if (!ok) return;
    try {
      await deleteProject(projectId);
      onDeleted();
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  if (loadError && !detail) {
    return <p className="mx-auto mt-16 max-w-2xl px-4 text-sm text-muted">{loadError}</p>;
  }
  if (!detail) return null;

  const usage = detail.max_tokens ? Math.min(1, detail.used_tokens / detail.max_tokens) : 0;
  const instructionsDirty = instructions.trim() !== detail.instructions;

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-16 pt-6">
        {/* 이름 */}
        <div className="mb-6 flex items-center gap-3">
          <FolderIcon className="h-7 w-7 shrink-0 text-muted" />
          <input
            value={name}
            maxLength={100}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => void saveName()}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
              if (e.key === "Escape") {
                setName(detail.name);
                requestAnimationFrame(() => e.currentTarget?.blur());
              }
            }}
            aria-label="프로젝트 이름"
            className="min-w-0 flex-1 rounded-lg bg-transparent px-1 font-display text-[28px] text-fg hover:bg-hover focus:bg-hover focus:outline-none"
          />
        </div>

        {/* 새 대화 */}
        {composer}
        <p className="mt-2 text-center text-[11px] text-subtle">
          이 프로젝트의 문서와 지시문을 바탕으로 새 대화를 시작합니다.
        </p>

        <div className="mt-10 grid gap-10 md:grid-cols-[1fr_280px]">
          {/* 대화 목록 */}
          <section>
            <h3 className="mb-2 text-sm font-medium text-fg">대화</h3>
            {detail.conversations.length === 0 ? (
              <p className="text-sm text-subtle">아직 대화가 없습니다.</p>
            ) : (
              <ul className="divide-y divide-line rounded-xl border border-line">
                {detail.conversations.map((c: Conversation) => (
                  <li key={c.id}>
                    <button
                      onClick={() => onOpenConversation(c.id)}
                      className="flex w-full items-baseline justify-between gap-3 px-3.5 py-2.5 text-left text-sm hover:bg-hover"
                    >
                      <span className="truncate text-fg">{c.title}</span>
                      <span className="shrink-0 text-[11px] text-subtle">
                        {new Date(c.created_at).toLocaleDateString()}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <aside className="space-y-8">
            {/* 문서 */}
            <section>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-medium text-fg">문서</h3>
                <div className="flex items-center gap-1">
                  {detail.documents.length > 0 && !processing && (
                    <button
                      onClick={() => void reprocessAll()}
                      className="rounded-lg px-2 py-1 text-xs text-muted hover:bg-hover hover:text-fg"
                      title="파서를 고친 뒤 저장된 원본으로 모든 문서를 다시 읽습니다"
                    >
                      모두 다시 처리
                    </button>
                  )}
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted hover:bg-hover hover:text-fg"
                  >
                    <UploadIcon className="h-3.5 w-3.5" /> 올리기
                  </button>
                </div>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  accept={documentExtensions.map((ext) => `.${ext}`).join(",")}
                  onChange={(e) => {
                    if (e.target.files) void upload(e.target.files);
                    e.target.value = "";
                  }}
                />
              </div>
              <div className="mb-3">
                <div className="h-1.5 overflow-hidden rounded-full bg-hover">
                  <div
                    className={`h-full rounded-full ${usage > 0.9 ? "bg-red-500" : "bg-fg/60"}`}
                    style={{ width: `${usage * 100}%` }}
                  />
                </div>
                <p className="mt-1 text-[11px] text-subtle">
                  용량 {Math.round(usage * 100)}% 사용 (약 {detail.used_tokens.toLocaleString()} /{" "}
                  {detail.max_tokens.toLocaleString()} 토큰)
                </p>
              </div>
              {detail.documents.length === 0 && uploading === 0 ? (
                <p className="text-sm text-subtle">
                  문서를 올리면 이 프로젝트의 모든 대화에서 참고합니다. 파일을 끌어다 놓아도 됩니다.
                </p>
              ) : (
                <div className="space-y-2">
                  {detail.documents.map((doc) => (
                    <div key={doc.id}>
                      <FileChip
                        name={doc.filename}
                        meta={projectDocumentMeta(doc)}
                        status={doc.status}
                        error={doc.error}
                        href={doc.status === "ready" ? documentFileUrl(doc.id) : undefined}
                        onRemove={() => void removeDocument(doc)}
                        removeLabel="문서 삭제"
                      />
                      {fallbackNote(doc) && (
                        <p className="mt-1 px-1 text-[11px] text-subtle">{fallbackNote(doc)}</p>
                      )}
                      {(doc.status === "ready" || doc.status === "failed") && (
                        <button
                          onClick={() => void reprocessDocument(doc)}
                          className="mt-0.5 px-1 text-[11px] text-subtle hover:text-fg"
                          title="저장된 원본으로 이 문서를 처음부터 다시 읽습니다"
                        >
                          다시 처리
                        </button>
                      )}
                    </div>
                  ))}
                  {uploading > 0 && (
                    <FileChip name={`${uploading}개 올리는 중`} status="uploading" />
                  )}
                </div>
              )}
            </section>

            {/* 지시문 */}
            <section>
              <h3 className="mb-2 text-sm font-medium text-fg">지시문</h3>
              <textarea
                value={instructions}
                maxLength={20000}
                onChange={(e) => setInstructions(e.target.value)}
                rows={5}
                placeholder="이 프로젝트의 모든 대화에서 지킬 내용"
                className="w-full resize-y rounded-xl border border-line bg-surface px-3 py-2 text-sm leading-relaxed text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none"
              />
              {instructionsDirty && (
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    onClick={() => setInstructions(detail.instructions)}
                    className="rounded-lg px-2.5 py-1 text-xs text-muted hover:bg-hover hover:text-fg"
                  >
                    되돌리기
                  </button>
                  <button
                    onClick={() => void saveInstructions()}
                    disabled={savingInstructions}
                    className="rounded-lg bg-fg px-2.5 py-1 text-xs font-medium text-app disabled:opacity-40"
                  >
                    저장
                  </button>
                </div>
              )}
            </section>

            <button
              onClick={() => void remove()}
              className="text-xs text-subtle hover:text-red-500"
            >
              프로젝트 삭제
            </button>
          </aside>
        </div>
      </div>
    </div>
  );
}
