"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "@/lib/api";
import { LogoIcon } from "./icons";
import { Sources } from "./Sources";
import { Steps } from "./Steps";

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

function AssistantMessage({ message, active }: { message: ChatMessage; active: boolean }) {
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
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        {showDots && <TypingDots />}
        <Sources steps={steps} />
      </div>
    </div>
  );
}

/**
 * 대화 메시지 목록.
 * activeId 는 지금 응답을 받고 있는 assistant 메시지 (진행 표시를 그 메시지에만 보인다).
 */
export function MessageList({
  messages,
  activeId = null,
}: {
  messages: ChatMessage[];
  activeId?: string | null;
}) {
  return (
    <div className="space-y-7">
      {messages.map((m) =>
        m.role === "user" ? (
          <UserMessage key={m.id} message={m} />
        ) : (
          <AssistantMessage key={m.id} message={m} active={m.id === activeId} />
        ),
      )}
    </div>
  );
}
