"use client";

import "katex/dist/katex.min.css";
import ReactMarkdown, { type Options as MarkdownOptions } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { type ChatMessage, documentFileUrl } from "@/lib/api";
import { documentMeta } from "@/lib/documents";
import { normalizeMath } from "@/lib/math";
import { FileChip } from "./FileChip";
import { LogoIcon } from "./icons";
import { Sources } from "./Sources";
import { Steps } from "./Steps";

// 수식: `$` 한 개는 금액과 헷갈리므로 끈다 (lib/math.ts 가 \( \) 를 $$ $$ 로 바꾼다).
// 문법이 틀린 수식은 오류 대신 원문을 빨갛게 보여준다.
const REMARK_PLUGINS: MarkdownOptions["remarkPlugins"] = [remarkGfm, [remarkMath, { singleDollarTextMath: false }]];
const REHYPE_PLUGINS: MarkdownOptions["rehypePlugins"] = [[rehypeKatex, { throwOnError: false }]];

function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-2" aria-label="응답 생성 중">
      {[0, 0.15, 0.3].map((d) => (
        <span
          key={d}
          className="typing-dot h-1.5 w-1.5 rounded-full bg-muted"
          style={{ animationDelay: `${d}s` }}
        />
      ))}
    </span>
  );
}

function UserMessage({ message }: { message: ChatMessage }) {
  return (
    <div className="flex flex-col items-end gap-2">
      {message.documents && message.documents.length > 0 && (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-2">
          {message.documents.map((d) => (
            <FileChip
              key={d.id}
              name={d.filename}
              meta={documentMeta(d)}
              href={documentFileUrl(d.id)}
            />
          ))}
        </div>
      )}
      {message.images && message.images.length > 0 && (
        <div className="flex flex-wrap justify-end gap-2">
          {message.images.map((src) => (
            <a key={src} href={src} target="_blank" rel="noreferrer">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={src}
                alt="첨부 이미지"
                className="max-h-56 max-w-56 rounded-xl border border-line object-cover"
              />
            </a>
          ))}
        </div>
      )}
      {message.content && (
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-bubble px-4 py-2.5 text-[15px] leading-relaxed">
          {message.content}
        </div>
      )}
    </div>
  );
}

function AssistantMessage({
  message,
  active,
  modelLabel,
}: {
  message: ChatMessage;
  active: boolean;
  modelLabel?: string;
}) {
  const steps = message.steps ?? [];
  const last = steps.at(-1);
  // 진행 중인 단계(생각 중, 도구 실행 중)가 보이면 대기 점은 띄우지 않는다.
  const stepInProgress =
    (message.thinking ?? false) || (last?.type === "tool" && last.status === "running");
  const showDots = active && !message.content && !stepInProgress;

  return (
    <div className="flex gap-3">
      <LogoIcon className="mt-0.5 h-6 w-6 shrink-0 text-muted" />
      <div className="min-w-0 flex-1">
        {steps.length > 0 && <Steps steps={steps} thinking={active && (message.thinking ?? false)} />}
        {message.content && (
          <div className="prose prose-neutral max-w-none text-[15px] leading-relaxed text-fg dark:prose-invert prose-p:my-2.5 prose-pre:rounded-xl prose-pre:border prose-pre:border-line prose-code:before:content-none prose-code:after:content-none">
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS}>
              {normalizeMath(message.content)}
            </ReactMarkdown>
          </div>
        )}
        {showDots && <TypingDots />}
        <Sources steps={steps} />
        {!active && modelLabel && (
          <div className="mt-2 text-[11px] text-subtle" title={message.model ?? undefined}>
            {modelLabel}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 대화 메시지 목록.
 * activeId 는 지금 응답을 받고 있는 assistant 메시지 (진행 표시를 그 메시지에만 보인다).
 * modelLabels 는 모델 ID → 표시 이름. 목록에 없는 모델(지금 쓸 수 없는 모델)은 모델 이름을 보여준다.
 */
export function MessageList({
  messages,
  activeId = null,
  modelLabels = {},
}: {
  messages: ChatMessage[];
  activeId?: string | null;
  modelLabels?: Record<string, string>;
}) {
  return (
    <div className="space-y-7">
      {messages.map((m) =>
        m.role === "user" ? (
          <UserMessage key={m.id} message={m} />
        ) : (
          <AssistantMessage
            key={m.id}
            message={m}
            active={m.id === activeId}
            modelLabel={
              m.model ? (modelLabels[m.model] ?? m.model.slice(m.model.indexOf(":") + 1)) : undefined
            }
          />
        ),
      )}
    </div>
  );
}
