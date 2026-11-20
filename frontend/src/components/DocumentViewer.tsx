"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import ReactMarkdown, { type Options as MarkdownOptions } from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_URL, type DocumentBlock, type DocumentView, documentFileUrl, getDocumentView } from "@/lib/api";
import { kindLabel } from "@/lib/documents";
import { CloseIcon, SpinnerIcon } from "./icons";

/** 문서 보기 패널에 띄울 문서. chunkId 가 있으면 그 청크(검색 결과)의 위치를 강조한다. */
export interface ViewerTarget {
  projectId: string;
  documentId: string;
  title: string;
  chunkId?: string | null;
  page?: number | null; // 청크 ID 가 없는 예전 출처: 이 쪽으로 스크롤한다
}

/** 같은 카드를 다시 눌러도 다시 스크롤하도록 여는 횟수를 붙인다. */
export type OpenedTarget = ViewerTarget & { opened: number };

const OpenDocumentContext = createContext<((target: ViewerTarget) => void) | null>(null);

/** 하위 컴포넌트(출처 카드, 프로젝트 문서 칩)가 문서 보기 패널을 열 수 있게 한다. */
export const OpenDocumentProvider = OpenDocumentContext.Provider;

/** 문서 보기 패널을 여는 함수. 패널이 없는 화면에서는 ``null``. */
export function useOpenDocument() {
  return useContext(OpenDocumentContext);
}

/** 출처 위치("p.3 · 1장")에서 첫 쪽 번호를 꺼낸다. */
export function pageOfLocation(location: string): number | null {
  const match = /^p\.(\d+)/.exec(location);
  return match ? Number(match[1]) : null;
}

// 문서 본문은 수식 변환 없이 표(GFM)만 읽는다. PDF 글자의 `$` 등이 수식으로 바뀌지 않게.
const REMARK_PLUGINS: MarkdownOptions["remarkPlugins"] = [remarkGfm];
const PROSE =
  "prose prose-sm prose-neutral max-w-none text-[13.5px] leading-relaxed text-fg dark:prose-invert prose-p:my-1.5 prose-table:my-2 prose-table:text-[12.5px]";

// 형광펜으로 찾을 때 무시할 글자: 공백, 그리고 Markdown 렌더링에서 사라지는 기호.
const IGNORED = /[\s*_`#>|\\]/;
const MARK_NAME = "doc-chunk";

/** 블록 안의 글자에서 청크 본문이 있는 범위를 찾는다 (공백·Markdown 기호 차이는 무시). */
function findRange(root: Element, text: string): Range | null {
  const needle = text.split("").filter((c) => !IGNORED.test(c)).join("");
  if (!needle) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const positions: { node: Text; offset: number }[] = [];
  let haystack = "";
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    for (let i = 0; i < node.data.length; i++) {
      if (IGNORED.test(node.data[i])) continue;
      haystack += node.data[i];
      positions.push({ node, offset: i });
    }
  }
  const at = haystack.indexOf(needle);
  if (at < 0) return null;
  const start = positions[at];
  const end = positions[at + needle.length - 1];
  const range = document.createRange();
  range.setStart(start.node, start.offset);
  range.setEnd(end.node, end.offset + 1);
  return range;
}

/** CSS Custom Highlight API 로 범위를 표시한다 (DOM 을 바꾸지 않는다). 지원하지 않는 브라우저는 건너뛴다. */
function setMark(range: Range | null) {
  const registry = (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
  const HighlightClass = (globalThis as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;
  if (!registry || !HighlightClass) return;
  if (range) registry.set(MARK_NAME, new HighlightClass(range));
  else registry.delete(MARK_NAME);
}

function Block({ block, highlighted }: { block: DocumentBlock; highlighted: boolean }) {
  return (
    <div
      data-block-id={block.id}
      className={`-mx-2 rounded-lg px-2 py-1 transition-colors ${
        highlighted ? "bg-amber-300/25 ring-1 ring-amber-500/40 dark:bg-amber-300/10" : ""
      }`}
    >
      {block.kind === "image" ? (
        <figure className="my-1">
          {block.image_url && (
            <a href={`${API_URL}${block.image_url}`} target="_blank" rel="noopener noreferrer">
              {/* eslint-disable-next-line @next/next/no-img-element -- 로그인 쿠키가 필요한 API 그림 */}
              <img
                src={`${API_URL}${block.image_url}`}
                alt="문서 속 그림"
                loading="lazy"
                className="max-h-80 max-w-full rounded-lg border border-line bg-white object-contain"
              />
            </a>
          )}
          <figcaption className="mt-1.5">
            <span className="text-[11px] font-medium text-subtle">
              {block.described ? "그림 설명" : "그림 속 글자 (OCR)"}
            </span>
            <div className={PROSE}>
              <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{block.content}</ReactMarkdown>
            </div>
          </figcaption>
        </figure>
      ) : (
        <div className={PROSE}>
          <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{block.content}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

/**
 * 오른쪽 문서 보기 패널: 프로젝트 문서의 추출 내용(모델이 읽은 것과 같은 내용)을 보여주고,
 * 출처 카드에서 열었으면 근거가 된 부분으로 스크롤해서 강조한다.
 * 넓은 화면은 대화 옆에 붙고, 좁은 화면은 전체를 덮는다.
 */
export function DocumentViewer({ target, onClose }: { target: OpenedTarget; onClose: () => void }) {
  const [view, setView] = useState<DocumentView | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const requestKey = `${target.projectId}:${target.documentId}:${target.chunkId ?? ""}`;

  useEffect(() => {
    const controller = new AbortController();
    getDocumentView(target.projectId, target.documentId, target.chunkId, controller.signal)
      .then((v) => {
        setView(v);
        setError(null);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        setError({ key: requestKey, message: e instanceof Error ? e.message : String(e) });
      });
    return () => controller.abort();
  }, [target.projectId, target.documentId, target.chunkId, requestKey]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 다른 문서를 불러오는 동안에는 이전 문서를 보여주지 않는다 (같은 문서의 다른 위치는 그대로 두고 강조만 옮긴다).
  const current = view && view.id === target.documentId ? view : null;
  const highlight = current?.highlight ?? null;
  const highlighted = new Set(highlight?.block_ids ?? []);

  // 근거 위치로 스크롤하고, 긴 본문을 나눈 조각이면 그 부분을 형광펜으로 한 번 더 표시한다.
  useEffect(() => {
    const box = bodyRef.current;
    if (!current || !box) return;
    const ids = current.highlight?.block_ids ?? [];
    const page = current.highlight?.page ?? target.page ?? null;
    const block = ids.length
      ? box.querySelector<HTMLElement>(`[data-block-id="${ids[0]}"]`)
      : null;
    const range =
      block && ids.length === 1 && current.highlight?.text
        ? findRange(block, current.highlight.text)
        : null;
    setMark(range);
    if (range) {
      const top = range.getBoundingClientRect().top - box.getBoundingClientRect().top;
      box.scrollTo({ top: box.scrollTop + top - box.clientHeight / 3 });
    } else if (block) {
      block.scrollIntoView({ block: "center" });
    } else if (page !== null) {
      box.querySelector<HTMLElement>(`[data-page="${page}"]`)?.scrollIntoView({ block: "start" });
    } else {
      box.scrollTop = 0;
    }
    return () => setMark(null);
  }, [current, target.page, target.opened]);

  const failed = error && error.key === requestKey ? error.message : null;
  let lastPage: number | null = null;
  let lastHeading: string | null = null;

  return (
    <aside
      className="fixed inset-0 z-40 flex flex-col bg-app md:static md:z-auto md:w-[min(600px,42vw)] md:shrink-0 md:border-l md:border-line"
      aria-label="문서 보기"
    >
      <header className="flex items-center gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-medium text-fg" title={current?.filename ?? target.title}>
            {current?.filename ?? target.title}
          </h2>
          <p className="truncate text-[11px] text-subtle">
            {current
              ? [kindLabel(current.kind), current.page_count ? `${current.page_count}쪽` : null, "추출한 내용"]
                  .filter(Boolean)
                  .join(" · ")
              : "불러오는 중"}
          </p>
        </div>
        <a
          href={documentFileUrl(target.documentId)}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-xs text-muted hover:bg-hover hover:text-fg"
        >
          원본 받기
        </a>
        <button
          type="button"
          onClick={onClose}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"
          aria-label="문서 보기 닫기"
          title="닫기 (Esc)"
        >
          <CloseIcon className="h-3.5 w-3.5" />
        </button>
      </header>

      <div ref={bodyRef} className="flex-1 overflow-y-auto px-5 pb-10 pt-2">
        {failed ? (
          <p className="mt-6 text-sm text-red-600 dark:text-red-300">{failed}</p>
        ) : !current ? (
          <div className="mt-6 flex items-center gap-2 text-sm text-subtle">
            <SpinnerIcon /> 불러오는 중
          </div>
        ) : current.blocks.length === 0 ? (
          <p className="mt-6 text-sm text-subtle">
            {current.status === "ready" ? "추출한 내용이 없습니다." : "아직 문서를 처리하는 중입니다."}
          </p>
        ) : (
          <>
            {highlight && highlight.block_ids.length === 0 && highlight.page !== null && (
              <p className="mt-2 rounded-lg bg-hover px-3 py-2 text-[12px] text-muted">
                근거가 이 화면에 따로 보이지 않는 표에 있습니다. p.{highlight.page} 로 이동했습니다.
              </p>
            )}
            {current.blocks.map((block) => {
              const newPage = block.page !== null && block.page !== lastPage;
              const newHeading = block.heading !== null && block.heading !== lastHeading;
              if (block.page !== null) lastPage = block.page;
              lastHeading = block.heading;
              return (
                <div key={block.id}>
                  {newPage && (
                    <div
                      data-page={block.page}
                      className="flex scroll-mt-2 items-center gap-2 pb-1 pt-4 text-[11px] text-subtle"
                    >
                      <span>p.{block.page}</span>
                      <span className="h-px flex-1 bg-line" />
                    </div>
                  )}
                  {newHeading && (
                    <p className="mt-2 text-[11px] font-medium text-muted">
                      {block.heading?.replaceAll("**", "")}
                    </p>
                  )}
                  <Block block={block} highlighted={highlighted.has(block.id)} />
                </div>
              );
            })}
          </>
        )}
      </div>
    </aside>
  );
}
