"use client";

import "katex/dist/katex.min.css";
import { useMemo, useState } from "react";
import ReactMarkdown, { type Components, type Options as MarkdownOptions } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { type ChatMessage, documentFileUrl } from "@/lib/api";
import { documentMeta } from "@/lib/documents";
import { citationNumber, citedNumbers, remarkCitations } from "@/lib/citations";
import { normalizeMath } from "@/lib/math";
import { FileChip } from "./FileChip";
import { LogoIcon, ThumbDownIcon, ThumbUpIcon } from "./icons";
import { EditedFiles } from "./EditedFiles";
import { Citation, collectSources, Sources } from "./Sources";
import { Steps } from "./Steps";

// 수식: `$` 한 개는 금액과 헷갈리므로 끈다 (lib/math.ts 가 \( \) 를 $$ $$ 로 바꾼다).
// 문법이 틀린 수식은 오류 대신 원문을 빨갛게 보여준다.
// 출처 번호([3])는 링크로 바꿔 칩으로 그린다 (lib/citations.ts).
const REMARK_PLUGINS: MarkdownOptions["remarkPlugins"] = [
  remarkGfm,
  [remarkMath, { singleDollarTextMath: false }],
  remarkCitations,
];
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
  onFeedback,
}: {
  message: ChatMessage;
  active: boolean;
  modelLabel?: string;
  onFeedback?: FeedbackHandler;
}) {
  const steps = message.steps ?? [];
  const last = steps.at(-1);
  // 진행 중인 단계(생각 중, 도구 실행 중)가 보이면 대기 점은 띄우지 않는다.
  const stepInProgress =
    (message.thinking ?? false) || (last?.type === "tool" && last.status === "running");
  const showDots = active && !message.content && !stepInProgress;
  const cited = useMemo(() => citedNumbers(message.content ?? ""), [message.content]);
  // 답변 속 [3] 을 같은 번호의 출처 카드와 잇는다.
  const components = useMemo<Components>(() => {
    const byNumber = new Map(collectSources(steps).map(({ n, source }) => [n, source]));
    return {
      a: ({ href, children, node: _node, ...rest }) => {
        const n = citationNumber(href);
        if (n !== null) return <Citation n={n} source={byNumber.get(n)} />;
        return (
          <a href={href} {...rest}>
            {children}
          </a>
        );
      },
    };
  }, [steps]);

  return (
    <div className="flex gap-3">
      <LogoIcon className="mt-0.5 h-6 w-6 shrink-0 text-muted" />
      <div className="min-w-0 flex-1">
        {steps.length > 0 && <Steps steps={steps} thinking={active && (message.thinking ?? false)} />}
        {message.content && (
          <div className="prose prose-neutral max-w-none text-[15px] leading-relaxed text-fg dark:prose-invert prose-p:my-2.5 prose-pre:rounded-xl prose-pre:border prose-pre:border-line prose-code:before:content-none prose-code:after:content-none">
            <ReactMarkdown
              remarkPlugins={REMARK_PLUGINS}
              rehypePlugins={REHYPE_PLUGINS}
              components={components}
            >
              {normalizeMath(message.content)}
            </ReactMarkdown>
          </div>
        )}
        {showDots && <TypingDots />}
        <EditedFiles steps={steps} />
        {/* 답변이 끝난 뒤에 인용한 출처를 앞으로 정리한다 (만드는 중에는 카드가 움직이지 않게 순서대로) */}
        <Sources steps={steps} cited={active ? null : cited} />
        {!active && (modelLabel || (onFeedback && message.serverId)) && (
          <div className="mt-2 flex items-start gap-3">
            {/* 저장된 응답에만 피드백을 단다 (서버 메시지 ID 가 있는 것) */}
            {onFeedback && message.serverId && <Feedback message={message} onFeedback={onFeedback} />}
            {modelLabel && (
              <span className="pt-1 text-[11px] text-subtle" title={message.model ?? undefined}>
                {modelLabel}
              </span>
            )}
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
/** 피드백을 보내는 함수: 같은 값을 다시 누르면 null (취소). */
type FeedbackHandler = (message: ChatMessage, value: 1 | -1 | null, comment?: string | null) => void;

const FEEDBACK_BUTTON =
  "flex h-6 w-6 items-center justify-center rounded-md hover:bg-hover hover:text-fg aria-pressed:text-fg";

/**
 * 응답 아래 👍/👎. 같은 버튼을 다시 누르면 취소한다. 👎 를 고르면 의견(선택)을 남길 수 있다.
 * 피드백은 응답의 Langfuse 트레이스에 점수로 남는다.
 */
function Feedback({ message, onFeedback }: { message: ChatMessage; onFeedback: FeedbackHandler }) {
  const [commenting, setCommenting] = useState(false);
  const [comment, setComment] = useState("");
  const value = message.feedback ?? null;

  function choose(next: 1 | -1) {
    const v = value === next ? null : next;
    onFeedback(message, v, null);
    setCommenting(v === -1);
    setComment("");
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-0.5 text-subtle">
        <button
          type="button"
          onClick={() => choose(1)}
          aria-pressed={value === 1}
          className={`${FEEDBACK_BUTTON} ${value === 1 ? "bg-hover" : ""}`}
          aria-label="좋은 응답"
          title="좋은 응답"
        >
          <ThumbUpIcon />
        </button>
        <button
          type="button"
          onClick={() => choose(-1)}
          aria-pressed={value === -1}
          className={`${FEEDBACK_BUTTON} ${value === -1 ? "bg-hover" : ""}`}
          aria-label="아쉬운 응답"
          title="아쉬운 응답"
        >
          <ThumbDownIcon />
        </button>
      </div>
      {commenting && value === -1 ? (
        <form
          className="flex max-w-md items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (comment.trim()) onFeedback(message, -1, comment.trim());
            setCommenting(false);
          }}
        >
          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            maxLength={2000}
            autoFocus
            placeholder="무엇이 아쉬웠나요? (선택)"
            className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-2.5 py-1 text-xs text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none"
          />
          <button type="submit" className="rounded-lg px-2 py-1 text-xs text-muted hover:bg-hover hover:text-fg">
            보내기
          </button>
          <button
            type="button"
            onClick={() => setCommenting(false)}
            className="rounded-lg px-2 py-1 text-xs text-subtle hover:bg-hover hover:text-fg"
          >
            닫기
          </button>
        </form>
      ) : (
        value === -1 &&
        message.feedbackComment && (
          <p className="max-w-md truncate text-[11px] text-subtle" title={message.feedbackComment}>
            의견: {message.feedbackComment}
          </p>
        )
      )}
    </div>
  );
}

export function MessageList({
  messages,
  activeId = null,
  modelLabels = {},
  onFeedback,
}: {
  messages: ChatMessage[];
  activeId?: string | null;
  modelLabels?: Record<string, string>;
  onFeedback?: FeedbackHandler;
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
            onFeedback={onFeedback}
          />
        ),
      )}
    </div>
  );
}
