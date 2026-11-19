"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "@/components/Composer";
import { LogoIcon, PlusIcon, SidebarIcon } from "@/components/icons";
import { MessageList } from "@/components/MessageList";
import { Sidebar } from "@/components/Sidebar";
import {
  type AppInfo,
  type ChatMessage,
  type Conversation,
  deleteConversation,
  getConversation,
  getInfo,
  listConversations,
  streamChat,
} from "@/lib/api";
import { type Attachment, MAX_IMAGES, fileToAttachment } from "@/lib/images";

const SIDEBAR_KEY = "sidebar-open";

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

  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

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
  }, [messages]);

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

  async function openConversation(id: string) {
    if (streaming) return;
    setError(null);
    try {
      const conv = await getConversation(id);
      jumpToBottomRef.current = true;
      setConversationId(conv.id);
      setMessages(conv.messages);
      setAttachments([]);
      if (isMobile) setSidebarOpen(false);
    } catch (e) {
      setError(String(e));
    }
  }

  function newConversation() {
    if (streaming) return;
    setConversationId(null);
    setMessages([]);
    setAttachments([]);
    setInput("");
    setError(null);
    setHello(greeting());
    if (isMobile) setSidebarOpen(false);
  }

  async function removeConversation(id: string) {
    await deleteConversation(id);
    if (id === conversationId) newConversation();
    refreshConversations();
  }

  async function send() {
    const text = input.trim();
    if ((!text && attachments.length === 0) || streaming) return;

    const sending = attachments;
    setInput("");
    setAttachments([]);
    setError(null);
    setStreaming(true);

    const assistantId = crypto.randomUUID();
    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: "user",
        content: text,
        images: sending.map((a) => a.previewUrl),
      },
      { id: assistantId, role: "assistant", content: "" },
    ]);

    const appendToAssistant = (chunk: string) =>
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + chunk } : m)),
      );

    const controller = new AbortController();
    abortRef.current = controller;
    const isNew = conversationId === null;

    try {
      await streamChat(
        {
          conversation_id: conversationId,
          message: text,
          images: sending.map((a) => ({ mime_type: a.mimeType, data: a.base64 })),
        },
        {
          onMeta: ({ conversation_id }) => setConversationId(conversation_id),
          onDelta: appendToAssistant,
          onDone: ({ stop_reason }) => {
            if (stop_reason === "refusal") appendToAssistant("\n\n_(요청이 거절되었습니다.)_");
            if (stop_reason === "max_tokens") appendToAssistant("\n\n_(응답이 길어 잘렸습니다.)_");
          },
          onError: setError,
        },
        controller.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(String(e));
    } finally {
      setStreaming(false);
      abortRef.current = null;
      if (isNew) refreshConversations();
    }
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
      onSend={send}
      onStop={() => abortRef.current?.abort()}
      streaming={streaming}
      modelLabel={info?.model}
      visionEnabled={info?.vision ?? true}
      autoFocus={isEmpty}
      placeholder={isEmpty ? "무엇이든 물어보세요" : "답글 보내기"}
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
                <MessageList messages={messages} />
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
    </div>
  );
}
