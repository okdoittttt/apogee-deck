"use client";

import { useState } from "react";
import { projectImageUrl } from "@/lib/api";
import type { DocumentSource, Source, Step, WebSource } from "@/lib/api";
import { pageOfLocation, useOpenDocument } from "./DocumentViewer";
import { FileIcon, GlobeIcon } from "./icons";

/** 처음에 보여줄 출처 수. 나머지는 "더 보기" 로 펼친다. */
const INITIAL_COUNT = 4;

function sourceKey(source: Source): string {
  return source.type === "document"
    ? `doc:${source.document_id}:${source.location}:${source.image_element_id ?? ""}`
    : `web:${source.url}`;
}

/** 응답의 도구 단계에서 출처를 모은다. 여러 번 찾았으면 순서대로 합치고 같은 출처는 한 번만 넣는다. */
function collectSources(steps: Step[]): Source[] {
  const seen = new Set<string>();
  const sources: Source[] = [];
  for (const step of steps) {
    if (step.type !== "tool") continue;
    for (const source of step.sources ?? []) {
      // 백엔드에서 걸렀지만, 링크로 쓰는 값이라 http(s) 만 한 번 더 확인한다.
      if (source.type !== "document" && !/^https?:\/\//.test(source.url)) continue;
      const key = sourceKey(source);
      if (seen.has(key)) continue;
      seen.add(key);
      sources.push(source);
    }
  }
  return sources;
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
      onClick={() =>
        openDocument?.({
          projectId: source.project_id,
          documentId: source.document_id,
          title: source.title,
          chunkId: source.chunk_id ?? null,
          page: pageOfLocation(source.location),
        })
      }
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
  const onlyDocuments = sources.every((s) => s.type === "document");

  return (
    <section className="mt-4" aria-label="출처">
      <div className="mb-2 flex items-center gap-1.5 text-xs text-subtle">
        {onlyDocuments ? <FileIcon className="h-3.5 w-3.5" /> : <GlobeIcon className="h-3.5 w-3.5" />}
        출처 {sources.length}
      </div>
      <ol className="grid gap-2 sm:grid-cols-2">
        {visible.map((source, i) => (
          <li key={sourceKey(source)} className="min-w-0">
            {source.type === "document" ? (
              <DocumentCard source={source} index={i + 1} />
            ) : (
              <WebCard source={source} index={i + 1} />
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
