"use client";

import { useState } from "react";
import { projectImageUrl } from "@/lib/api";
import type { ViewerTarget } from "./DocumentViewer";
import type { DocumentSource, Source, Step, WebSource } from "@/lib/api";
import { pageOfLocation, useOpenDocument } from "./DocumentViewer";
import { FileIcon, GlobeIcon } from "./icons";

/** 처음에 보여줄 출처 수. 나머지는 "더 보기" 로 펼친다. */
const INITIAL_COUNT = 4;

/** 번호가 붙은 출처. 번호는 답변 속 `[3]` 과 같다. */
export interface NumberedSource {
  n: number;
  source: Source;
}

function sourceKey(source: Source): string {
  // 번호가 있으면 같은 번호가 같은 출처다 (한 턴에서 다시 나온 출처는 앞의 번호를 쓴다).
  if (source.n) return `n:${source.n}`;
  return source.type === "document"
    ? `doc:${source.document_id}:${source.location}:${source.image_element_id ?? ""}`
    : `web:${source.url}`;
}

/**
 * 응답의 도구 단계에서 출처를 모은다. 여러 번 찾았으면 합치고 같은 출처는 한 번만 넣는다.
 * 서버가 매긴 번호(n)가 있으면 그 번호 순서로, 없으면(예전 메시지) 나온 순서대로 번호를 매긴다.
 */
export function collectSources(steps: Step[]): NumberedSource[] {
  const seen = new Set<string>();
  const sources: NumberedSource[] = [];
  for (const step of steps) {
    if (step.type !== "tool") continue;
    for (const source of step.sources ?? []) {
      // 백엔드에서 걸렀지만, 링크로 쓰는 값이라 http(s) 만 한 번 더 확인한다.
      if (source.type !== "document" && !/^https?:\/\//.test(source.url)) continue;
      const key = sourceKey(source);
      if (seen.has(key)) continue;
      seen.add(key);
      sources.push({ n: source.n ?? sources.length + 1, source });
    }
  }
  return sources.sort((a, b) => a.n - b.n);
}

/** 문서 출처를 문서 보기 패널에 띄울 대상으로 바꾼다. */
function viewerTarget(source: DocumentSource): ViewerTarget {
  return {
    projectId: source.project_id,
    documentId: source.document_id,
    title: source.title,
    chunkId: source.chunk_id ?? null,
    page: pageOfLocation(source.location),
  };
}

const CITATION_CLASS =
  "mx-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-md bg-hover px-1 align-[2px] font-sans text-[10.5px] font-medium leading-none text-muted no-underline hover:bg-active hover:text-fg";

/**
 * 답변 속 출처 번호 칩. 프로젝트 문서는 문서 보기 패널에서 근거 위치를 열고, 웹은 새 탭에서 연다.
 * 맞는 출처가 없으면(모델이 없는 번호를 쓴 경우) 글자 그대로 둔다.
 */
export function Citation({ n, source }: { n: number; source?: Source }) {
  const openDocument = useOpenDocument();
  if (!source) return <>[{n}]</>;
  if (source.type === "document") {
    return (
      <button
        type="button"
        disabled={!openDocument}
        onClick={() => openDocument?.(viewerTarget(source))}
        className={CITATION_CLASS}
        title={source.location ? `${source.title} · ${source.location}` : source.title}
        aria-label={`출처 ${n}: ${source.title}`}
      >
        {n}
      </button>
    );
  }
  return (
    <a
      href={source.url}
      target="_blank"
      rel="noopener noreferrer"
      className={CITATION_CLASS}
      title={`${source.title}\n${hostname(source.url)}`}
      aria-label={`출처 ${n}: ${source.title}`}
    >
      {n}
    </a>
  );
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function WebCard({ source, index }: { source: WebSource; index: number }) {
  return (
    <a
      href={source.url}
      target="_blank"
      rel="noopener noreferrer"
      className="flex h-full gap-2.5 rounded-xl border border-line px-3 py-2 hover:bg-hover"
      title={source.title}
    >
      <span className="mt-px shrink-0 font-mono text-[11px] text-subtle">{index}</span>
      <span className="min-w-0">
        <span className="line-clamp-2 text-[13px] leading-snug text-fg">{source.title}</span>
        <span className="mt-0.5 block truncate text-[11px] text-subtle">
          {hostname(source.url)}
          {source.published_date && ` · ${source.published_date}`}
        </span>
      </span>
    </a>
  );
}

/** 프로젝트 문서 출처: 문서 이름과 위치. 누르면 문서 보기 패널에서 근거 위치를 보여준다. */
function DocumentCard({ source, index }: { source: DocumentSource; index: number }) {
  const openDocument = useOpenDocument();
  const image = source.image_element_id
    ? projectImageUrl(source.project_id, source.image_element_id)
    : null;
  const label = source.location ? `${source.title} · ${source.location}` : source.title;
  return (
    <button
      type="button"
      disabled={!openDocument}
      onClick={() => openDocument?.(viewerTarget(source))}
      className="flex h-full w-full gap-2.5 rounded-xl border border-line px-3 py-2 text-left enabled:hover:bg-hover"
      title={`${label}\n눌러서 문서에서 보기`}
    >
      <span className="mt-px shrink-0 font-mono text-[11px] text-subtle">{index}</span>
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-[13px] leading-snug text-fg">{source.title}</span>
        <span className="mt-0.5 block truncate text-[11px] text-subtle">
          {image ? "그림" : "본문"}
          {source.location && ` · ${source.location}`}
        </span>
      </span>
      {image && (
        // eslint-disable-next-line @next/next/no-img-element -- 로그인 쿠키가 필요한 API 그림
        <img
          src={image}
          alt={`${source.title} 그림`}
          className="h-10 w-14 shrink-0 rounded-md border border-line object-cover"
        />
      )}
    </button>
  );
}

/**
 * 답변 아래에 도구(웹 검색, 프로젝트 문서 검색)가 참고한 출처를 목록으로 보여준다.
 * 모델이 쓴 출처가 아니라 실제 검색 결과라서, 모델이 출처를 빠뜨리거나 지어내도 정확하다.
 */
export function Sources({ steps }: { steps: Step[] }) {
  const [expanded, setExpanded] = useState(false);
  const sources = collectSources(steps);
  if (sources.length === 0) return null;
  const visible = expanded ? sources : sources.slice(0, INITIAL_COUNT);
  const hidden = sources.length - visible.length;
  const onlyDocuments = sources.every(({ source }) => source.type === "document");

  return (
    <section className="mt-4" aria-label="출처">
      <div className="mb-2 flex items-center gap-1.5 text-xs text-subtle">
        {onlyDocuments ? <FileIcon className="h-3.5 w-3.5" /> : <GlobeIcon className="h-3.5 w-3.5" />}
        출처 {sources.length}
      </div>
      <ol className="grid gap-2 sm:grid-cols-2">
        {visible.map(({ n, source }) => (
          <li key={sourceKey(source)} className="min-w-0">
            {source.type === "document" ? (
              <DocumentCard source={source} index={n} />
            ) : (
              <WebCard source={source} index={n} />
            )}
          </li>
        ))}
      </ol>
      {(hidden > 0 || expanded) && sources.length > INITIAL_COUNT && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-2 text-xs text-muted hover:text-fg"
        >
          {expanded ? "접기" : `${hidden}개 더 보기`}
        </button>
      )}
    </section>
  );
}
