"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "@/components/Composer";
import { LogoIcon, PlusIcon, SidebarIcon } from "@/components/icons";
import { MessageList } from "@/components/MessageList";
import { Sidebar } from "@/components/Sidebar";
import {
  type QueuedMessage,
  QueuedMessageBubble,
  RemoteRunningNotice,
} from "@/components/StatusNotices";
import { Toast } from "@/components/Toast";
import {
  type AppInfo,
  type ChatMessage,
  type Conversation,
  type Step,
  type ToolStep,
  ERROR_SUFFIX,
  INTERRUPTED_SUFFIX,
  NO_REPLY_TEXT,
  deleteConversation,
  getConversation,
  getConversationStatus,
  getInfo,
  listConversations,
  streamChat,
} from "@/lib/api";
import { type Attachment, MAX_IMAGES, fileToAttachment } from "@/lib/images";

const SIDEBAR_KEY = "sidebar-open";
const STATUS_POLL_MS = 2000;
// 중지 직후 서버가 정리(부분 응답 저장, 생성 중 표시 해제)를 끝낼 때까지 기다리는 간격과 한도
const IDLE_POLL_MS = 150;
const IDLE_WAIT_MAX_MS = 10000;
const QUEUE_FULL_TEXT = "응답 생성 중에는 메시지를 1개까지만 대기열에 넣을 수 있습니다.";
const QUEUE_PENDING_TEXT = "대기 메시지를 먼저 보내거나 삭제해 주세요.";
const NAV_BLOCKED_TEXT = "응답과 대기 메시지가 모두 끝난 뒤에 이동할 수 있습니다.";
const REMOTE_BUSY_TEXT = "이전 응답이 아직 생성 중입니다. 완료된 후 다시 시도해 주세요.";

type Outcome = "done" | "error" | "aborted" | "ended" | "busy";

function greeting(): string {
  const h = new Date().getHours();
  if (h < 6) return "늦은 시간까지 수고 많으세요";
  if (h < 12) return "좋은 아침이에요";
  if (h < 18) return "좋은 오후예요";
  return "좋은 저녁이에요";
}

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isMobile, setIsMobile] = useState(false);
  const [hello, setHello] = useState("무엇을 도와드릴까요?");
  // 같은 창: 응답 생성 중 입력한 메시지 (최대 1개). 오류가 나면 자동 전송을 멈춘다.
  const [queued, setQueuedState] = useState<QueuedMessage | null>(null);
  const [queuePaused, setQueuePaused] = useState(false);
  // 다른 창에서 이 대화의 응답을 생성 중인지
  const [remoteRunning, setRemoteRunning] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // 지금 응답을 받고 있는 assistant 메시지 (진행 표시를 이 메시지에만 보인다)
  const [activeAssistantId, setActiveAssistantId] = useState<string | null>(null);
  const closeToast = useCallback(() => setToast(null), []);

  // 응답이 끝난 뒤 대기 메시지를 자동 전송할 때 최신 값을 읽기 위한 ref.
  // (state 를 그대로 읽으면 이전 렌더의 값이라, 새 대화의 ID 가 없는 것으로 보여 대화가 하나 더 생긴다)
  const conversationIdRef = useRef<string | null>(null);
  const queuedRef = useRef<QueuedMessage | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  function setActiveConversation(id: string | null) {
    conversationIdRef.current = id;
    setConversationId(id);
  }

  function setQueued(message: QueuedMessage | null) {
    queuedRef.current = message;
    setQueuedState(message);
    if (!message) setQueuePaused(false);
  }

  const refreshConversations = useCallback(() => {
    listConversations()
      .then(setConversations)
      .catch((e) => setError(String(e)));
  }, []);

  // 초기 로드: 대화 목록, 모델 정보, 화면 크기에 따른 사이드바 상태
  useEffect(() => {
    refreshConversations();
    getInfo()
      .then(setInfo)
      .catch(() => setInfo(null));
    setHello(greeting());

    const mq = window.matchMedia("(max-width: 767px)");
    const apply = () => {
      setIsMobile(mq.matches);
      if (mq.matches) {
        setSidebarOpen(false);
      } else {
        try {
          setSidebarOpen(localStorage.getItem(SIDEBAR_KEY) !== "false");
        } catch {
          setSidebarOpen(true);
        }
      }
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [refreshConversations]);

  // ⌘K / Ctrl+K: 대화 검색
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSidebarOpen(true);
        requestAnimationFrame(() => searchRef.current?.focus());
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 대화를 열 때는 즉시 맨 아래로, 응답 스트리밍 중에는 부드럽게 따라간다.
  const jumpToBottomRef = useRef(false);
  useEffect(() => {
    const behavior = jumpToBottomRef.current ? "auto" : "smooth";
    jumpToBottomRef.current = false;
    bottomRef.current?.scrollIntoView({ behavior, block: "end" });
  }, [messages, queued, remoteRunning]);

  // 다른 창의 응답이 끝날 때까지 주기적으로 확인하고, 끝나면 대화를 다시 불러온다.
  useEffect(() => {
    if (!remoteRunning || !conversationId) return;
    const id = conversationId;
    const timer = setInterval(async () => {
      try {
        const { running } = await getConversationStatus(id);
        if (running || conversationIdRef.current !== id) return;
        const conv = await getConversation(id);
        if (conversationIdRef.current !== id) return;
        jumpToBottomRef.current = true;
        setMessages(conv.messages);
        setRemoteRunning(conv.running);
      } catch {
        setRemoteRunning(false); // 대화가 삭제됨 등
      }
    }, STATUS_POLL_MS);
    return () => clearInterval(timer);
  }, [remoteRunning, conversationId]);

  function toggleSidebar(open: boolean) {
    setSidebarOpen(open);
    if (!isMobile) {
      try {
        localStorage.setItem(SIDEBAR_KEY, String(open));
      } catch {}
    }
  }

  async function addFiles(files: Iterable<File>) {
    const images = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (images.length === 0) return;
    const room = MAX_IMAGES - attachments.length;
    if (images.length > room) setError(`이미지는 최대 ${MAX_IMAGES}장까지 첨부할 수 있습니다.`);
    if (room <= 0) return;
    try {
      const added = await Promise.all(images.slice(0, room).map(fileToAttachment));
      setAttachments((prev) => [...prev, ...added].slice(0, MAX_IMAGES));
    } catch (e) {
      setError(String(e));
    }
  }

  /** 응답 생성 중이거나 대기 메시지가 있으면 다른 대화로 이동하지 않는다. */
  function navigationBlocked(): boolean {
    if (streaming || queuedRef.current) {
      setToast(NAV_BLOCKED_TEXT);
      return true;
    }
    return false;
  }

  async function openConversation(id: string) {
    if (navigationBlocked()) return;
    setError(null);
    try {
      const conv = await getConversation(id);
      jumpToBottomRef.current = true;
      setActiveConversation(conv.id);
      setMessages(conv.messages);
      setRemoteRunning(conv.running);
      setAttachments([]);
      if (isMobile) setSidebarOpen(false);
    } catch (e) {
      setError(String(e));
    }
  }

  function newConversation() {
    if (navigationBlocked()) return;
    setActiveConversation(null);
    setRemoteRunning(false);
    setMessages([]);
    setAttachments([]);
    setInput("");
    setError(null);
    setHello(greeting());
    if (isMobile) setSidebarOpen(false);
  }

  async function removeConversation(id: string) {
    if (id === conversationIdRef.current && (streaming || queuedRef.current)) {
      setToast(NAV_BLOCKED_TEXT);
      return;
    }
    try {
      await deleteConversation(id);
    } catch (e) {
      setToast((e as Error).message); // 다른 창에서 응답 생성 중이면 서버가 거절
      return;
    }
    if (id === conversationIdRef.current) newConversation();
    refreshConversations();
  }

  /**
   * 입력창의 메시지를 처리한다.
   * - 다른 창에서 응답 생성 중: 보내지 않고 안내
   * - 이 창에서 응답 생성 중: 대기열에 넣음 (1개까지, 그 이상은 거절)
   * - 그 외: 바로 전송
   */
  function submit() {
    const text = input.trim();
    if (!text && attachments.length === 0) return;
    if (remoteRunning) {
      setToast(REMOTE_BUSY_TEXT);
      return;
    }
    if (queuedRef.current) {
      setToast(streaming ? QUEUE_FULL_TEXT : QUEUE_PENDING_TEXT);
      return;
    }
    const message: QueuedMessage = { id: crypto.randomUUID(), text, attachments };
    setInput("");
    setAttachments([]);
    if (streaming) {
      setQueued(message);
    } else {
      void runSend(message);
    }
  }

  /** 대기 메시지를 지금 보낸다 (오류로 자동 전송이 멈췄을 때). */
  function sendQueuedNow() {
    const next = queuedRef.current;
    if (!next || streaming) return;
    setQueued(null);
    void runSend(next);
  }

  async function runSend(message: QueuedMessage) {
    const { text, attachments: sending } = message;
    setError(null);
    setStreaming(true);

    const userId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    setMessages((prev) => [
      ...prev,
      { id: userId, role: "user", content: text, images: sending.map((a) => a.previewUrl) },
      { id: assistantId, role: "assistant", content: "" },
    ]);

    setActiveAssistantId(assistantId);

    const updateAssistant = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? fn(m) : m)));
    const appendToAssistant = (chunk: string) =>
      updateAssistant((m) => ({ ...m, content: m.content + chunk, thinking: false }));
    // 연속된 추론 조각은 마지막 추론 단계에 이어 붙인다.
    const appendReasoning = (chunk: string) =>
      updateAssistant((m) => {
        const steps = m.steps ?? [];
        const last = steps.at(-1);
        const merged: Step[] =
          last?.type === "reasoning"
            ? [...steps.slice(0, -1), { ...last, text: last.text + chunk }]
            : [...steps, { type: "reasoning", text: chunk }];
        return { ...m, steps: merged, thinking: true };
      });
    const updateTool = (id: string, patch: Partial<ToolStep>) =>
      updateAssistant((m) => ({
        ...m,
        steps: (m.steps ?? []).map((st) =>
          st.type === "tool" && st.id === id ? { ...st, ...patch } : st,
        ),
      }));

    const controller = new AbortController();
    abortRef.current = controller;
    const isNew = conversationIdRef.current === null;
    // 응답이 어떻게 끝났는지. done 이벤트 없이 스트림이 닫히면(연결 끊김) "ended".
    // 콜백 안에서 바뀌므로 TS 가 "ended" 로만 좁히지 않게 타입을 명시한다.
    let outcome = "ended" as Outcome;

    try {
      await streamChat(
        {
          conversation_id: conversationIdRef.current,
          message: text,
          images: sending.map((a) => ({ mime_type: a.mimeType, data: a.base64 })),
        },
        {
          onMeta: ({ conversation_id }) => setActiveConversation(conversation_id),
          onDelta: appendToAssistant,
          onReasoning: appendReasoning,
          onToolStart: (tool) =>
            updateAssistant((m) => ({
              ...m,
              thinking: false,
              steps: [
                ...(m.steps ?? []),
                { type: "tool", ...tool, output: null, status: "running" },
              ],
            })),
          onToolEnd: ({ id, output, status, sources }) =>
            updateTool(id, { output, status, sources }),
          onDone: ({ stop_reason }) => {
            outcome = "done";
            if (stop_reason === "refusal") appendToAssistant("\n\n_(요청이 거절되었습니다.)_");
            if (stop_reason === "max_tokens") appendToAssistant("\n\n_(응답이 길어 잘렸습니다.)_");
          },
          onError: (msg) => {
            outcome = "error";
            setError(msg);
          },
          onBusy: (detail) => {
            // 다른 창에서 생성 중이라 거절됨: 메시지는 저장되지 않았으니 화면에서 되돌린다.
            outcome = "busy";
            setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== assistantId));
            setInput((prev) => prev || text);
            setAttachments((prev) => (prev.length ? prev : sending));
            setRemoteRunning(true);
            setToast(detail || REMOTE_BUSY_TEXT);
          },
        },
        controller.signal,
      );
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        outcome = "aborted";
      } else {
        outcome = "error";
        setError(String(e));
      }
    } finally {
      if (outcome !== "busy") finishAssistantMessage(assistantId, outcome);
      setActiveAssistantId(null);
      setStreaming(false);
      abortRef.current = null;
      if (isNew) refreshConversations();
    }

    // 대기 메시지 처리: 정상 종료·중지면 바로 이어서 보내고, 오류면 멈추고 버튼을 보여준다.
    if (!queuedRef.current) return;
    if (outcome === "error" || outcome === "busy") {
      setQueuePaused(true);
      return;
    }
    if (outcome !== "done" && !(await waitUntilIdle(conversationIdRef.current))) {
      setQueuePaused(true);
      setToast("이전 응답의 정리가 늦어지고 있습니다. 잠시 후 대기 메시지를 보내 주세요.");
      return;
    }
    const next = queuedRef.current; // 기다리는 동안 사용자가 지웠을 수 있다
    if (!next) return;
    setQueued(null);
    void runSend(next);
  }

  /**
   * 중지하거나 연결이 끊긴 직후, 서버가 이전 응답을 정리할 때까지 기다린다.
   * 서버는 연결이 끊긴 것을 알아챈 뒤 부분 응답을 저장하고 생성 중 표시를 해제하는데,
   * 그 전에 다음 메시지를 보내면 "다른 창의 요청"으로 거절되고 저장 순서도 뒤바뀐다.
   * (정상 종료는 서버가 done 을 보내기 전에 해제하므로 기다릴 필요가 없다.)
   */
  async function waitUntilIdle(id: string | null): Promise<boolean> {
    if (!id) return true; // 대화가 만들어지기 전에 중지됨
    const deadline = Date.now() + IDLE_WAIT_MAX_MS;
    while (Date.now() < deadline) {
      try {
        if (!(await getConversationStatus(id)).running) return true;
      } catch {
        return true; // 대화가 없으면 기다릴 것도 없다
      }
      await new Promise((r) => setTimeout(r, IDLE_POLL_MS));
    }
    return false;
  }

  /**
   * 응답 자리를 결과에 맞게 정리한다. 비어 있는 채로 "생각 중" 표시가 남지 않게 하고,
   * 실행 중으로 남은 도구는 중단/오류로 바꾼다 (서버가 저장하는 내용과 같게).
   */
  function finishAssistantMessage(id: string, outcome: Exclude<Outcome, "busy">) {
    setMessages((prev) =>
      prev.flatMap((m) => {
        if (m.id !== id) return [m];
        const toolStatus: ToolStep["status"] = outcome === "error" ? "error" : "interrupted";
        const steps = (m.steps ?? []).map((st) =>
          st.type === "tool" && st.status === "running" ? { ...st, status: toolStatus } : st,
        );
        const base = { ...m, steps, thinking: false };
        const empty = m.content === "";
        switch (outcome) {
          case "done":
            return [empty && steps.length === 0 ? { ...base, content: NO_REPLY_TEXT } : base];
          case "error":
            // 보여준 것이 없으면 자리를 지운다 (오류 내용은 오류 상자에 표시됨).
            if (empty && steps.length === 0) return [];
            return [{ ...base, content: empty ? ERROR_SUFFIX.trim() : m.content + ERROR_SUFFIX }];
          default: // 사용자가 중지했거나 연결이 끊김
            return [
              {
                ...base,
                content: empty ? INTERRUPTED_SUFFIX.trim() : m.content + INTERRUPTED_SUFFIX,
              },
            ];
        }
      }),
    );
  }

  const isEmpty = messages.length === 0;
  const activeTitle = conversations.find((c) => c.id === conversationId)?.title;

  const composer = (
    <Composer
      value={input}
      onChange={setInput}
      attachments={attachments}
      onAddFiles={addFiles}
      onRemoveAttachment={(id) => setAttachments((prev) => prev.filter((a) => a.id !== id))}
      onSend={submit}
      onStop={() => abortRef.current?.abort()}
      streaming={streaming}
      queueFull={queued !== null}
      modelLabel={info?.model}
      visionEnabled={info?.vision ?? true}
      autoFocus={isEmpty}
      placeholder={
        isEmpty
          ? "무엇이든 물어보세요"
          : streaming && !queued
            ? "응답이 끝나면 보낼 메시지를 입력하세요 (1개까지)"
            : "답글 보내기"
      }
    />
  );

  const errorBox = error && (
    <div className="mb-3 flex items-start justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-600 dark:text-red-300">
      <span className="break-words">{error}</span>
      <button onClick={() => setError(null)} className="shrink-0 opacity-70 hover:opacity-100">
        닫기
      </button>
    </div>
  );

  return (
    <div className="flex h-dvh overflow-hidden bg-app text-fg">
      {/* Sidebar: 데스크톱은 밀어내기, 모바일은 오버레이 */}
      {sidebarOpen && isMobile && (
        <div className="fixed inset-0 z-30 bg-black/40" onClick={() => toggleSidebar(false)} />
      )}
      <div
        className={`${isMobile ? "fixed inset-y-0 left-0 z-40" : "relative"} transition-[margin] duration-200 ${
          sidebarOpen ? "ml-0" : "-ml-[264px]"
        }`}
      >
        <Sidebar
          ref={searchRef}
          conversations={conversations}
          activeId={conversationId}
          info={info}
          onNew={newConversation}
          onOpen={openConversation}
          onDelete={removeConversation}
          onClose={() => toggleSidebar(false)}
        />
      </div>

      <main
        className="relative flex min-w-0 flex-1 flex-col"
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes("Files")) {
            e.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={(e) => {
          if (e.currentTarget === e.target) setDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (info?.vision ?? true) addFiles(e.dataTransfer.files);
        }}
      >
        {/* Top bar */}
        <header className="flex h-14 shrink-0 items-center gap-1 px-3">
          {!sidebarOpen && (
            <>
              <button
                onClick={() => toggleSidebar(true)}
                className="rounded-lg p-1.5 text-muted hover:bg-hover hover:text-fg"
                aria-label="사이드바 열기"
                title="사이드바 열기"
              >
                <SidebarIcon />
              </button>
              <button
                onClick={newConversation}
                className="rounded-lg p-1.5 text-muted hover:bg-hover hover:text-fg"
                aria-label="새 대화"
                title="새 대화"
              >
                <PlusIcon />
              </button>
            </>
          )}
          {!isEmpty && activeTitle && (
            <h1 className="ml-1 truncate text-sm font-medium text-muted">{activeTitle}</h1>
          )}
        </header>

        {dragging && (
          <div className="pointer-events-none absolute inset-3 z-20 flex items-center justify-center rounded-2xl border-2 border-dashed border-line-strong bg-app/85 text-muted">
            이미지를 여기에 놓으세요
          </div>
        )}

        {isEmpty ? (
          /* 빈 화면: 가운데 인사말 + 입력창 */
          <div className="flex flex-1 flex-col items-center justify-center px-4 pb-[12vh]">
            <div className="mb-8 flex items-center gap-3">
              <LogoIcon className="h-9 w-9 text-fg" />
              <h2 className="font-display text-[32px] font-normal leading-tight text-fg">
                {hello}
              </h2>
            </div>
            <div className="w-full max-w-[680px]">
              {errorBox}
              {composer}
              <p className="mt-3 text-center text-xs text-subtle">
                이미지를 붙여넣거나 끌어다 놓을 수 있어요 · Shift+Enter 로 줄바꿈
              </p>
            </div>
          </div>
        ) : (
          /* 대화 화면: 메시지 목록 + 하단 고정 입력창 */
          <>
            <div className="flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-3xl px-4 pb-6 pt-2">
                <MessageList messages={messages} activeId={activeAssistantId} />
                {queued && (
                  <QueuedMessageBubble
                    message={queued}
                    paused={queuePaused}
                    onSendNow={sendQueuedNow}
                    onRemove={() => setQueued(null)}
                  />
                )}
                {remoteRunning && <RemoteRunningNotice />}
                <div ref={bottomRef} />
              </div>
            </div>
            <div className="mx-auto w-full max-w-3xl px-4 pb-4">
              {errorBox}
              {composer}
              <p className="mt-2 text-center text-[11px] text-subtle">
                AI 응답은 부정확할 수 있습니다. 중요한 정보는 확인하세요.
              </p>
            </div>
          </>
        )}
      </main>
      <Toast message={toast} onClose={closeToast} />
    </div>
  );
}
