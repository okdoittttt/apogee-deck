"use client";

import { useState } from "react";
import type { Source, Step } from "@/lib/api";
import { GlobeIcon } from "./icons";

/** 처음에 보여줄 출처 수. 나머지는 "더 보기" 로 펼친다. */
const INITIAL_COUNT = 4;

/** 응답의 도구 단계에서 출처를 모은다. 여러 번 검색했으면 순서대로 합치고 같은 URL 은 한 번만 넣는다. */
function collectSources(steps: Step[]): Source[] {
  const seen = new Set<string>();
  const sources: Source[] = [];
  for (const step of steps) {
    if (step.type !== "tool") continue;
    for (const source of step.sources ?? []) {
      // 백엔드에서 걸렀지만, 링크로 쓰는 값이라 http(s) 만 한 번 더 확인한다.
      if (!/^https?:\/\//.test(source.url) || seen.has(source.url)) continue;
      seen.add(source.url);
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

/**
 * 답변 아래에 도구(웹 검색 등)가 참고한 페이지를 링크 목록으로 보여준다.
 * 모델이 쓴 출처가 아니라 실제 검색 결과라서, 모델이 출처를 빠뜨리거나 지어내도 정확하다.
 */
export function Sources({ steps }: { steps: Step[] }) {
  const [expanded, setExpanded] = useState(false);
  const sources = collectSources(steps);
  if (sources.length === 0) return null;
  const visible = expanded ? sources : sources.slice(0, INITIAL_COUNT);
  const hidden = sources.length - visible.length;

  return (
    <section className="mt-4" aria-label="출처">
      <div className="mb-2 flex items-center gap-1.5 text-xs text-subtle">
        <GlobeIcon className="h-3.5 w-3.5" />
        출처 {sources.length}
      </div>
      <ol className="grid gap-2 sm:grid-cols-2">
        {visible.map((source, i) => (
          <li key={source.url} className="min-w-0">
            <a
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-full gap-2.5 rounded-xl border border-line px-3 py-2 hover:bg-hover"
              title={source.title}
            >
              <span className="mt-px shrink-0 font-mono text-[11px] text-subtle">{i + 1}</span>
              <span className="min-w-0">
                <span className="line-clamp-2 text-[13px] leading-snug text-fg">{source.title}</span>
                <span className="mt-0.5 block truncate text-[11px] text-subtle">
                  {hostname(source.url)}
                  {source.published_date && ` · ${source.published_date}`}
                </span>
              </span>
            </a>
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
