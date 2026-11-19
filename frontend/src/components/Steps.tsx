"use client";

import { useState } from "react";
import type { ReasoningStep, Step, ToolStep } from "@/lib/api";
import { BrainIcon, CheckIcon, ChevronIcon, CloseIcon, SpinnerIcon, ToolIcon } from "./icons";

/** 접고 펼 수 있는 실행 단계 한 줄. */
function Disclosure({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="group flex max-w-full items-center gap-2 rounded-md py-0.5 text-sm text-muted hover:text-fg"
        aria-expanded={open}
      >
        <span className="flex h-4 w-4 shrink-0 items-center justify-center">{icon}</span>
        <span className="truncate">{title}</span>
        <ChevronIcon
          className={`h-3.5 w-3.5 shrink-0 opacity-60 transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      {open && <div className="ml-2 mt-1.5 border-l border-line pl-4">{children}</div>}
    </div>
  );
}

function ReasoningView({ step, live }: { step: ReasoningStep; live: boolean }) {
  return (
    <Disclosure
      icon={live ? <SpinnerIcon /> : <BrainIcon />}
      title={live ? <span className="animate-pulse">생각하는 중…</span> : "생각 과정"}
    >
      <p className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words text-[13px] leading-relaxed text-muted">
        {step.text.trim()}
      </p>
    </Disclosure>
  );
}

const STATUS_ICON: Record<ToolStep["status"], React.ReactNode> = {
  running: <SpinnerIcon />,
  done: <CheckIcon className="h-3.5 w-3.5 text-emerald-500" />,
  error: <CloseIcon className="h-3 w-3 text-red-500" />,
  interrupted: <CloseIcon className="h-3 w-3 text-subtle" />,
};

function toolTitle(step: ToolStep): string {
  switch (step.status) {
    case "running":
      return `${step.label}…`;
    case "done":
      return step.done_label;
    case "error":
      return `${step.label} · 실패`;
    case "interrupted":
      return `${step.label} · 중단됨`;
  }
}

function ToolView({ step }: { step: ToolStep }) {
  const args = Object.keys(step.args).length ? JSON.stringify(step.args, null, 2) : "(없음)";
  return (
    <Disclosure
      icon={STATUS_ICON[step.status]}
      title={
        <>
          {toolTitle(step)}
          <span className="ml-2 font-mono text-[11px] text-subtle">{step.name}</span>
        </>
      }
    >
      <div className="space-y-2 text-[13px]">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-xs text-subtle">
            <ToolIcon className="h-3 w-3" /> 입력
          </div>
          <pre className="overflow-x-auto rounded-lg bg-hover px-3 py-2 font-mono text-xs text-fg">
            {args}
          </pre>
        </div>
        <div>
          <div className="mb-1 text-xs text-subtle">결과</div>
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-hover px-3 py-2 font-mono text-xs text-fg">
            {step.output ?? (step.status === "running" ? "실행 중…" : "(결과 없음)")}
          </pre>
        </div>
      </div>
    </Disclosure>
  );
}

/**
 * 응답의 실행 단계(추론 과정, 도구 호출)를 일어난 순서대로 보여준다.
 * 기본은 접힌 상태이고, 눌러서 추론 내용이나 도구 입력·결과를 볼 수 있다.
 */
export function Steps({ steps, thinking }: { steps: Step[]; thinking: boolean }) {
  return (
    <div className="mb-3 space-y-1">
      {steps.map((step, i) =>
        step.type === "reasoning" ? (
          <ReasoningView key={i} step={step} live={thinking && i === steps.length - 1} />
        ) : (
          <ToolView key={step.id} step={step} />
        ),
      )}
    </div>
  );
}
