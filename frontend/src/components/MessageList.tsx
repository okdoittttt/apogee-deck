"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "@/lib/api";
import { LogoIcon } from "./icons";

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

function AssistantMessage({ message }: { message: ChatMessage }) {
  return (
    <div className="flex gap-3">
      <LogoIcon className="mt-0.5 h-6 w-6 shrink-0 text-muted" />
      <div className="min-w-0 flex-1">
        {message.content ? (
          <div className="prose prose-neutral max-w-none text-[15px] leading-relaxed text-fg dark:prose-invert prose-p:my-2.5 prose-pre:rounded-xl prose-pre:border prose-pre:border-line prose-code:before:content-none prose-code:after:content-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        ) : (
          <TypingDots />
        )}
      </div>
    </div>
  );
}

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="space-y-7">
      {messages.map((m) =>
        m.role === "user" ? (
          <UserMessage key={m.id} message={m} />
        ) : (
          <AssistantMessage key={m.id} message={m} />
        ),
      )}
    </div>
  );
}
