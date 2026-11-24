"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  type DocumentChanges,
  type DocumentVersion,
  documentFileUrl,
  getDocumentChanges,
  getDocumentFile,
  getDocumentVersions,
  restoreDocumentVersion,
} from "@/lib/api";
import { formatBytes, kindLabel } from "@/lib/documents";
import { CloseIcon, SpinnerIcon } from "./icons";

/** 수정본 패널에 띄울 문서 (에이전트가 만든 수정본·새 문서). */
export interface EditedTarget {
  documentId: string;
  filename: string;
  kind: string;
}

const OpenEditedContext = createContext<((target: EditedTarget) => void) | null>(null);

/** 답변 아래 파일 칩이 수정본 패널을 열 수 있게 한다. */
export const OpenEditedProvider = OpenEditedContext.Provider;

/** 수정본 패널을 여는 함수. 패널이 없는 화면에서는 ``null`` (칩은 바로 받기로 동작). */
export function useOpenEdited() {
  return useContext(OpenEditedContext);
}

type Tab = "preview" | "changes" | "versions";

const TABS: { id: Tab; label: string }[] = [
  { id: "preview", label: "미리보기" },
  { id: "changes", label: "바뀐 곳" },
  { id: "versions", label: "버전" },
];

const PROSE =
  "prose prose-sm prose-neutral max-w-none text-[13.5px] leading-relaxed text-fg dark:prose-invert prose-table:text-[12.5px]";

/** 오류 메시지 (예외가 아닌 값도 글자로). */
function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// --- 미리보기 ------------------------------------------------------------------------------------

/** 따옴표를 지키는 간단한 CSV 읽기 (미리보기용). */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += c;
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** A4 쪽 너비(px). 패널이 좁으면 이 너비를 패널에 맞춰 줄여 보여준다. */
const PAGE_WIDTH = 816;

/** Word 파일을 브라우저에서 그린다 (docx-preview). 실제 Word 와 조금 다를 수 있다. */
function DocxPreview({ blob }: { blob: Blob }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    let cancelled = false;
    const page = pageRef.current;
    if (!page) return;
    page.replaceChildren();
    import("docx-preview")
      .then(({ renderAsync }) =>
        renderAsync(blob, page, undefined, {
          className: "docx",
          inWrapper: true,
          breakPages: true,
          ignoreLastRenderedPageBreak: true,
          renderComments: true,
          renderChanges: true,
          experimental: true,
        }),
      )
      .then(() => {
        if (!cancelled) setError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(messageOf(e));
      });
    return () => {
      cancelled = true;
    };
  }, [blob]);

  // 패널 너비에 맞춰 쪽을 줄인다.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const observer = new ResizeObserver(() => {
      setZoom(Math.min(1, (box.clientWidth - 8) / PAGE_WIDTH));
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={boxRef} className="docx-preview-box">
      {error && (
        <p className="mb-2 text-sm text-red-600 dark:text-red-300">
          미리보기를 그리지 못했습니다: {error}
        </p>
      )}
      <div ref={pageRef} style={{ zoom }} />
    </div>
  );
}

function TextPreview({ text, kind }: { text: string; kind: string }) {
  if (kind === "md") {
    return (
      <div className={PROSE}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
      </div>
    );
  }
  if (kind === "csv") {
    const [head, ...rows] = parseCsv(text);
    return (
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr>
              {(head ?? []).map((cell, i) => (
                <th key={i} className="border border-line bg-hover px-2 py-1 text-left font-medium text-fg">
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, i) => (
                  <td key={i} className="border border-line px-2 py-1 align-top text-fg">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return <pre className="whitespace-pre-wrap break-words font-mono text-[12.5px] text-fg">{text}</pre>;
}

function Preview({ documentId, kind }: { documentId: string; kind: string }) {
  const [file, setFile] = useState<{ id: string; blob: Blob; text: string | null } | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDocumentFile(documentId, controller.signal)
      .then(async (blob) => {
        const text = kind === "docx" ? null : (await blob.text()).replace(/^﻿/, "");
        setFile({ id: documentId, blob, text });
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setError({ id: documentId, message: messageOf(e) });
      });
    return () => controller.abort();
  }, [documentId, kind]);

  if (error && error.id === documentId) return <p className="mt-6 text-sm text-red-600 dark:text-red-300">{error.message}</p>;
  if (!file || file.id !== documentId) return <Loading />;
  if (kind === "docx") {
    return (
      <>
        <p className="mb-2 text-[11px] text-subtle">미리보기는 실제 Word 화면과 조금 다를 수 있습니다.</p>
        <DocxPreview blob={file.blob} />
      </>
    );
  }
  return <TextPreview text={file.text ?? ""} kind={kind} />;
}

// --- 바뀐 곳 -------------------------------------------------------------------------------------

const BLOCK_STYLE: Record<string, string> = {
  changed: "border-amber-500/70",
  added: "border-emerald-500/70",
  removed: "border-red-500/70",
  same: "border-transparent",
};

const BLOCK_LABEL: Record<string, string> = { changed: "고침", added: "넣음", removed: "지움" };

function Changes({ documentId }: { documentId: string }) {
  const [result, setResult] = useState<{ id: string; data: DocumentChanges } | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getDocumentChanges(documentId, controller.signal)
      .then((data) => setResult({ id: documentId, data }))
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setError({ id: documentId, message: messageOf(e) });
      });
    return () => controller.abort();
  }, [documentId]);

  if (error && error.id === documentId) return <p className="mt-6 text-sm text-red-600 dark:text-red-300">{error.message}</p>;
  if (!result || result.id !== documentId) return <Loading />;
  const { data } = result;
  if (!data.previous) {
    return <p className="mt-6 text-sm text-subtle">처음 버전이라 비교할 직전 버전이 없습니다.</p>;
  }
  const unit = data.unit === "paragraph" ? "문단" : "줄";
  return (
    <div>
      <p className="text-[12px] text-muted">
        <span className="font-medium text-fg">{data.previous.filename}</span> 과 비교 ·{" "}
        {data.changed ? `바뀐 ${unit} ${data.changed}개` : `글자가 바뀐 ${unit}이 없습니다`}
      </p>
      <p className="mt-1 text-[11px] text-subtle">
        글자만 비교합니다. 굵게·색 같은 서식만 바꾼 곳은 여기에 보이지 않으니 미리보기에서 확인하세요.
      </p>
      <div className="mt-3 space-y-1">
        {data.blocks.map((block, i) =>
          block.kind === "skipped" ? (
            <div key={i} className="flex items-center gap-2 py-1 text-[11px] text-subtle">
              <span className="h-px flex-1 bg-line" />
              <span>
                같은 {unit} {block.skipped}개
              </span>
              <span className="h-px flex-1 bg-line" />
            </div>
          ) : (
            <div key={i} className={`border-l-2 pl-3 ${BLOCK_STYLE[block.kind]}`}>
              <div className="flex gap-1.5 text-[10.5px] text-subtle">
                <span>{block.label}</span>
                {block.where && <span>· {block.where}</span>}
                {BLOCK_LABEL[block.kind] && <span>· {BLOCK_LABEL[block.kind]}</span>}
              </div>
              <p className={`whitespace-pre-wrap break-words text-[13px] leading-relaxed ${block.kind === "same" ? "text-muted" : "text-fg"}`}>
                {block.parts.map((part, k) =>
                  part.op === "insert" ? (
                    <ins key={k} className="rounded-sm bg-emerald-500/20 text-emerald-800 no-underline dark:text-emerald-200">
                      {part.text}
                    </ins>
                  ) : part.op === "delete" ? (
                    <del key={k} className="rounded-sm bg-red-500/15 text-red-700 dark:text-red-300">
                      {part.text}
                    </del>
                  ) : (
                    <span key={k}>{part.text}</span>
                  ),
                )}
              </p>
            </div>
          ),
        )}
      </div>
    </div>
  );
}

// --- 버전 ----------------------------------------------------------------------------------------

function versionLabel(version: DocumentVersion): string {
  if (version.created_by === "agent") return version.source_document_id ? "에이전트가 고침" : "에이전트가 만듦";
  return version.source_document_id ? "예전 버전으로 되돌림" : "올린 원본";
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Versions({
  documentId,
  onShow,
  onRestored,
}: {
  documentId: string;
  onShow: (version: DocumentVersion) => void;
  onRestored: (version: DocumentVersion) => void;
}) {
  const [list, setList] = useState<DocumentVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    getDocumentVersions(documentId, signal)
      .then((versions) => {
        setList(versions);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!signal?.aborted) setError(messageOf(e));
      });
  }, [documentId]);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function restore(version: DocumentVersion) {
    const question = `${version.filename} 을(를) 복사해 새 버전으로 만들까요? 지금까지의 버전은 그대로 남습니다.`;
    if (!window.confirm(question)) return;
    setBusy(version.id);
    try {
      const created = await restoreDocumentVersion(version.id);
      onRestored(created);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(null);
    }
  }

  if (error) return <p className="mt-6 text-sm text-red-600 dark:text-red-300">{error}</p>;
  if (!list) return <Loading />;
  const latest = list[list.length - 1]?.id;
  return (
    <div>
      <p className="text-[11px] text-subtle">
        되돌리기는 그 버전을 복사해 가장 새 버전 뒤에 붙입니다. 예전 버전은 지워지지 않습니다.
      </p>
      <ol className="mt-3 space-y-2">
        {[...list].reverse().map((version) => (
          <li
            key={version.id}
            className={`rounded-xl border px-3 py-2.5 ${version.id === documentId ? "border-fg/30 bg-hover" : "border-line"}`}
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg" title={version.filename}>
                {version.filename}
              </span>
              {version.id === latest && (
                <span className="shrink-0 rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[10.5px] text-emerald-700 dark:text-emerald-300">
                  최신
                </span>
              )}
              {version.id === documentId && (
                <span className="shrink-0 rounded-md bg-hover px-1.5 py-0.5 text-[10.5px] text-muted">보는 중</span>
              )}
            </div>
            <p className="mt-0.5 text-[11px] text-muted">
              v{version.revision} · {versionLabel(version)} · {formatTime(version.created_at)} ·{" "}
              {formatBytes(version.size_bytes)}
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {version.id !== documentId && (
                <button type="button" onClick={() => onShow(version)} className={SMALL_BUTTON}>
                  보기
                </button>
              )}
              <a href={documentFileUrl(version.id)} className={SMALL_BUTTON}>
                받기
              </a>
              {version.id !== latest && (
                <button
                  type="button"
                  onClick={() => void restore(version)}
                  disabled={busy !== null}
                  className={SMALL_BUTTON}
                >
                  {busy === version.id ? "되돌리는 중…" : "이 버전으로 되돌리기"}
                </button>
              )}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

const SMALL_BUTTON =
  "rounded-lg border border-line px-2 py-0.5 text-[11.5px] text-muted hover:bg-hover hover:text-fg disabled:opacity-50";

function Loading() {
  return (
    <div className="mt-6 flex items-center gap-2 text-sm text-subtle">
      <SpinnerIcon /> 불러오는 중
    </div>
  );
}

// --- 패널 ----------------------------------------------------------------------------------------

/**
 * 에이전트가 만든 수정본·새 문서의 오른쪽 패널: 미리보기, 직전 버전과 바뀐 곳, 버전 목록과 되돌리기.
 * 넓은 화면은 대화 옆에 붙고, 좁은 화면은 전체를 덮는다.
 */
export function EditedDocumentPanel({
  target,
  onClose,
  onNotice,
}: {
  target: EditedTarget;
  onClose: () => void;
  onNotice: (message: string) => void;
}) {
  const [shown, setShown] = useState<EditedTarget>(target);
  const [tab, setTab] = useState<Tab>("preview");

  // 다른 칩을 누르면 그 문서로 바꾼다.
  const [opened, setOpened] = useState(target);
  if (opened !== target) {
    setOpened(target);
    setShown(target);
    setTab("preview");
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const show = (version: DocumentVersion) => {
    setShown({ documentId: version.id, filename: version.filename, kind: version.kind });
    setTab("preview");
  };

  return (
    <aside
      className="fixed inset-0 z-40 flex flex-col bg-app md:static md:z-auto md:w-[min(640px,45vw)] md:shrink-0 md:border-l md:border-line"
      aria-label="수정본 보기"
    >
      <header className="flex items-center gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-medium text-fg" title={shown.filename}>
            {shown.filename}
          </h2>
          <p className="truncate text-[11px] text-subtle">{kindLabel(shown.kind)}</p>
        </div>
        <a
          href={documentFileUrl(shown.documentId)}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-xs text-muted hover:bg-hover hover:text-fg"
        >
          받기
        </a>
        <button
          type="button"
          onClick={onClose}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"
          aria-label="수정본 보기 닫기"
          title="닫기 (Esc)"
        >
          <CloseIcon className="h-3.5 w-3.5" />
        </button>
      </header>
      <nav className="flex gap-1 border-b border-line px-3" role="tablist">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-2.5 py-2 text-[12.5px] ${
              tab === id ? "border-fg font-medium text-fg" : "border-transparent text-muted hover:text-fg"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className="flex-1 overflow-y-auto px-4 pb-10 pt-3">
        {tab === "preview" && <Preview documentId={shown.documentId} kind={shown.kind} />}
        {tab === "changes" && <Changes documentId={shown.documentId} />}
        {tab === "versions" && (
          <Versions
            documentId={shown.documentId}
            onShow={show}
            onRestored={(version) => {
              show(version);
              onNotice(`${version.filename} 을(를) 만들었습니다. 다음 수정은 이 버전에서 이어집니다.`);
            }}
          />
        )}
      </div>
    </aside>
  );
}
