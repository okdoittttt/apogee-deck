"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AuthGate } from "@/components/Auth";
import { Composer } from "@/components/Composer";
import {
  DocumentViewer,
  type OpenedTarget,
  OpenDocumentProvider,
  type ViewerTarget,
} from "@/components/DocumentViewer";
import { LogoIcon, PlusIcon, SidebarIcon } from "@/components/icons";
import { MessageList } from "@/components/MessageList";
import { ModelPicker } from "@/components/ModelPicker";
import { MemoryView } from "@/components/MemoryView";
import { NewProjectForm, ProjectView } from "@/components/ProjectView";
import { Sidebar } from "@/components/Sidebar";
import {
  type QueuedMessage,
  QueuedMessageBubble,
} from "@/components/StatusNotices";
import { Toast } from "@/components/Toast";
import {
  type AppInfo,
  type ChatMessage,
  type Conversation,
  type ModelOption,
  type Project,
  type Step,
  type ToolStep,
  type DocumentInfo,
  ERROR_SUFFIX,
  INTERRUPTED_SUFFIX,
  NO_REPLY_TEXT,
  deleteConversation,
  deleteDocument,
  getConversation,
  getConversationStatus,
  getDocument,
  getInfo,
  getModels,
  listConversations,
  listProjects,
  resumeStream,
  setFeedback,
  stopConversation,
  streamChat,
  type StreamHandlers,
  uploadDocument,
} from "@/lib/api";
import type { User } from "@/lib/auth";
import { type PendingDocument, extensionOf, formatBytes, isImageFile } from "@/lib/documents";
import { type Attachment, MAX_IMAGES, fileToAttachment } from "@/lib/images";
import { isMacPlatform, shortcutLabel } from "@/lib/platform";
import { type Route, conversationPath, parseRoute, projectPath, routePath } from "@/lib/routes";

const SIDEBAR_KEY = "sidebar-open";
const MODEL_KEY = "selected-model";
// 연결이 끊긴 응답을 다시 이어 받을 때: 시도 횟수와 첫 대기 시간 (시도마다 두 배)
const RESUME_RETRIES = 5;
const RESUME_DELAY_MS = 500;
// 보낸 메시지를 맨 위로 올릴 때 위쪽 여백, 목록 아래 여백 (메시지 목록의 pt-2, pb-6 과 같다)
const PIN_GAP_PX = 8;
const PIN_BOTTOM_PX = 24;
const DOCUMENT_POLL_MS = 1000;
// 중지 직후 서버가 정리(부분 응답 저장, 생성 중 표시 해제)를 끝낼 때까지 기다리는 간격과 한도
const IDLE_POLL_MS = 150;
const IDLE_WAIT_MAX_MS = 10000;
const QUEUE_FULL_TEXT = "응답 생성 중에는 메시지를 1개까지만 대기열에 넣을 수 있습니다.";
const QUEUE_PENDING_TEXT = "대기 메시지를 먼저 보내거나 삭제해 주세요.";
const NAV_BLOCKED_TEXT = "대기 메시지가 전송된 뒤에 이동할 수 있습니다.";
const APP_TITLE = "okdoitttt";

/** 화면을 바꿀 때 주소를 어떻게 남길지: 기록에 쌓기, 지금 기록을 바꾸기, 그대로 두기(주소를 따라 연 경우). */
type HistoryMode = "push" | "replace" | "none";
const REMOTE_BUSY_TEXT = "다른 창에서 응답을 만드는 중입니다. 진행 상황을 이어서 보여 드립니다.";

/**
 * 응답 스트림이 어떻게 끝났는지.
 * - ended: 끝 이벤트 없이 연결이 닫힘 (다시 이어 받는다) / gone: 이어 받을 실행이 없음 (저장된 결과를 다시 읽는다)
 * - detached: 다른 화면으로 옮겨 이 창만 연결을 끊음 (응답은 서버에서 계속 만들어진다)
 */
type Outcome = "done" | "error" | "aborted" | "ended" | "busy" | "detached" | "gone";

/** 응답 하나를 받는 동안의 상태: 결과와 마지막으로 받은 이벤트 번호. */
type ReplyState = { outcome: Outcome; lastId: number };

function greeting(): string {
  const h = new Date().getHours();
  if (h < 6) return "늦은 시간까지 수고 많으세요";
  if (h < 12) return "좋은 아침이에요";
  if (h < 18) return "좋은 오후예요";
  return "좋은 저녁이에요";
}

export default function ChatPage() {
  return (
    <AuthGate>
      {(user, me, onLogout) => (
        <ChatApp user={user} authRequired={me.auth_required} onLogout={onLogout} />
      )}
    </AuthGate>
  );
}

/** 승인된 사용자의 채팅 화면. */
function ChatApp({
  user,
  authRequired,
  onLogout,
}: {
  user: User;
  authRequired: boolean;
  onLogout: () => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  // 오른쪽 문서 보기 패널 (출처 카드, 프로젝트 문서 칩에서 연다)
  const [viewerTarget, setViewerTarget] = useState<OpenedTarget | null>(null);
  const openDocument = useCallback(
    (target: ViewerTarget) => setViewerTarget((prev) => ({ ...target, opened: (prev?.opened ?? 0) + 1 })),
    [],
  );
  const closeViewer = useCallback(() => setViewerTarget(null), []);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // 입력창에 붙인 문서. 올리면 서버가 백그라운드에서 읽고, 다 읽은 뒤에 보낼 수 있다.
  const [documents, setDocumentsState] = useState<PendingDocument[]>([]);
  const documentsRef = useRef<PendingDocument[]>([]);
  const [dragging, setDragging] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<AppInfo | null>(null);
  // 고를 수 있는 모델과 지금 고른 모델 (다음에 보내는 메시지부터 적용)
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [isMobile, setIsMobile] = useState(false);
  const [hello, setHello] = useState("무엇을 도와드릴까요?");
  // 같은 창: 응답 생성 중 입력한 메시지 (최대 1개). 오류가 나면 자동 전송을 멈춘다.
  const [queued, setQueuedState] = useState<QueuedMessage | null>(null);
  const [queuePaused, setQueuePaused] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // 지금 응답을 받고 있는 assistant 메시지 (진행 표시를 이 메시지에만 보인다)
  const [activeAssistantId, setActiveAssistantId] = useState<string | null>(null);
  const closeToast = useCallback(() => setToast(null), []);
  // 이 창에서 마지막으로 보낸 사용자 메시지. 이 메시지를 화면 맨 위에 두고 응답을 그 아래에 채운다.
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  // 프로젝트: 목록, 지금 보고 있는 프로젝트(프로젝트 화면이거나 프로젝트 안의 대화), 새 프로젝트 만들기 화면인지
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState<string | null>(null);
  const projectIdRef = useRef<string | null>(null);
  const [creatingProject, setCreatingProject] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  // 프로젝트 화면에 끌어다 놓은 파일은 입력창이 아니라 프로젝트 문서로 올린다 (ProjectView 가 등록).
  const projectDropRef = useRef<((files: FileList) => void) | null>(null);
  const registerProjectDrop = useCallback((handler: ((files: FileList) => void) | null) => {
    projectDropRef.current = handler;
  }, []);

  // 응답이 끝난 뒤 대기 메시지를 자동 전송할 때 최신 값을 읽기 위한 ref.
  // (state 를 그대로 읽으면 이전 렌더의 값이라, 새 대화의 ID 가 없는 것으로 보여 대화가 하나 더 생긴다)
  const conversationIdRef = useRef<string | null>(null);
  const queuedRef = useRef<QueuedMessage | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  // 다른 화면으로 옮겨 끊은 연결 (중지가 아니므로 응답 자리를 "중단됨" 으로 바꾸지 않는다)
  const detachedRef = useRef(new WeakSet<AbortController>());
  // 새 대화의 ID 를 받기 전에 중지를 누르면, ID 를 받자마자 중지한다.
  const stopRequestedRef = useRef(false);
  // 메시지 목록의 스크롤 영역과 그 높이 (보낸 메시지를 맨 위로 올릴 빈 공간을 계산한다)
  const [scrollBox, setScrollBox] = useState<HTMLDivElement | null>(null);
  const [scrollBoxHeight, setScrollBoxHeight] = useState(0);
  const pinnedTurnRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  function setActiveConversation(id: string | null) {
    conversationIdRef.current = id;
    setConversationId(id);
  }

  // 지금 화면의 주소. 뒤로 가기를 막아야 할 때(응답 생성 중) 이 주소로 되돌린다.
  const pathRef = useRef("/");

  /** 주소를 화면에 맞춘다 (lib/routes.ts). 같은 주소면 기록을 남기지 않는다. */
  function setUrl(path: string, mode: HistoryMode) {
    pathRef.current = path;
    if (mode === "none" || window.location.pathname === path) return;
    if (mode === "push") window.history.pushState(null, "", path);
    else window.history.replaceState(null, "", path);
  }

  function setProjectId(id: string | null) {
    projectIdRef.current = id;
    setProjectIdState(id);
  }

  function setDocuments(update: (prev: PendingDocument[]) => PendingDocument[]) {
    documentsRef.current = update(documentsRef.current);
    setDocumentsState(documentsRef.current);
  }

  const patchDocument = (localId: string, patch: Partial<PendingDocument>) =>
    setDocuments((prev) => prev.map((d) => (d.localId === localId ? { ...d, ...patch } : d)));

  /** 입력창의 문서를 모두 뺀다. 서버에 올라간 문서는 지운다 (보내지 않은 문서). */
  function discardDocuments() {
    for (const d of documentsRef.current) if (d.doc) void deleteDocument(d.doc.id);
    setDocuments(() => []);
  }

  function setQueued(message: QueuedMessage | null) {
    queuedRef.current = message;
    setQueuedState(message);
    if (!message) setQueuePaused(false);
  }

  /**
   * 모델 목록을 받는다. 고른 모델이 목록에 없으면(키를 지웠거나 Ollama 를 껐음)
   * 마지막으로 고른 모델 → 서버 기본 모델 → 첫 모델 순으로 고른다.
   */
  const refreshModels = useCallback(() => {
    setModelsLoading(true);
    getModels()
      .then(({ models: list, default: fallback }) => {
        setModels(list);
        const ids = new Set(list.map((m) => m.id));
        setSelectedModel((current) => {
          if (current && ids.has(current)) return current;
          let saved: string | null = null;
          try {
            saved = localStorage.getItem(MODEL_KEY);
          } catch {}
          if (saved && ids.has(saved)) return saved;
          return fallback ?? list[0]?.id ?? null;
        });
      })
      .catch(() => setModels([]))
      .finally(() => setModelsLoading(false));
  }, []);

  function chooseModel(id: string) {
    setSelectedModel(id);
    try {
      localStorage.setItem(MODEL_KEY, id);
    } catch {}
  }

  const refreshConversations = useCallback(() => {
    listConversations()
      .then(setConversations)
      .catch((e) => setError(String(e)));
  }, []);

  const refreshProjects = useCallback(() => {
    listProjects()
      .then(setProjects)
      .catch((e) => setError(String(e)));
  }, []);

  // 초기 로드: 대화 목록, 모델 정보, 화면 크기에 따른 사이드바 상태
  useEffect(() => {
    refreshConversations();
    refreshProjects();
    getInfo()
      .then(setInfo)
      .catch(() => setInfo(null));
    refreshModels();
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
  }, [refreshConversations, refreshProjects, refreshModels]);

  // 주소대로 화면을 연다: 처음 열 때 한 번, 뒤로·앞으로 가기마다.
  const openRouteRef = useRef<(route: Route) => void>(() => {});
  const blockedRef = useRef(false);
  useEffect(() => {
    openRouteRef.current = openRoute;
    blockedRef.current = queuedRef.current !== null;
  });
  useEffect(() => {
    pathRef.current = window.location.pathname;
    openRouteRef.current(parseRoute(window.location.pathname));
    const onPop = () => {
      if (blockedRef.current) {
        // 대기 메시지가 있으면 다른 화면으로 가지 않는다. 브라우저 뒤로 가기는 막을 수 없어서 주소를 되돌린다.
        window.history.pushState(null, "", pathRef.current);
        setToast(NAV_BLOCKED_TEXT);
        return;
      }
      openRouteRef.current(parseRoute(window.location.pathname));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 탭 제목: 대화 제목이나 프로젝트 이름 (탭과 방문 기록에서 구분하기 쉽게)
  useEffect(() => {
    const title = conversationId
      ? conversations.find((c) => c.id === conversationId)?.title
      : projectId
        ? projects.find((p) => p.id === projectId)?.name
        : creatingProject
          ? "새 프로젝트"
          : memoryOpen
            ? "기억"
            : null;
    document.title = title ? `${title} - ${APP_TITLE}` : APP_TITLE;
  }, [conversationId, conversations, projectId, projects, creatingProject, memoryOpen]);

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

  // ⌘N / Ctrl+N: 새 대화. Chrome 등 일부 브라우저는 이 키를 페이지에 넘기지 않고 새 창을 연다.
  // 그래서 어디서나 동작하는 ⇧⌘O / Ctrl+Shift+O 도 같이 받는다.
  const newConversationRef = useRef<() => void>(() => {});
  useEffect(() => {
    newConversationRef.current = newConversation;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.altKey || !(isMacPlatform() ? e.metaKey : e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if ((key === "n" && !e.shiftKey) || (key === "o" && e.shiftKey)) {
        e.preventDefault();
        newConversationRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 스크롤 영역 높이를 따라간다 (창 크기나 입력창 높이가 바뀌면 달라진다).
  useLayoutEffect(() => {
    if (!scrollBox) return;
    setScrollBoxHeight(scrollBox.clientHeight);
    const observer = new ResizeObserver(() => setScrollBoxHeight(scrollBox.clientHeight));
    observer.observe(scrollBox);
    return () => observer.disconnect();
  }, [scrollBox]);

  // 대화를 열면 즉시 맨 아래로 간다. 메시지를 보내면 그 메시지를 화면 맨 위로 올리고,
  // 응답은 그 아래에 채운다 (응답이 길어져도 따라 내려가지 않는다).
  const jumpToBottomRef = useRef(false);
  const scrollToPinnedRef = useRef(false);
  useEffect(() => {
    if (!scrollBox) return;
    if (jumpToBottomRef.current) {
      jumpToBottomRef.current = false;
      scrollBox.scrollTo({ top: scrollBox.scrollHeight });
      return;
    }
    const turn = pinnedTurnRef.current;
    if (!scrollToPinnedRef.current || !turn || scrollBoxHeight === 0) return;
    scrollToPinnedRef.current = false;
    const offset = turn.getBoundingClientRect().top - scrollBox.getBoundingClientRect().top;
    scrollBox.scrollTo({ top: scrollBox.scrollTop + offset - PIN_GAP_PX, behavior: "smooth" });
  }, [scrollBox, scrollBoxHeight, messages, pinnedId]);

  // 서버가 읽는 중인 문서의 상태를 주기적으로 확인한다.
  const documentsProcessing = documents.some((d) => d.status === "processing");
  useEffect(() => {
    if (!documentsProcessing) return;
    const timer = setInterval(() => {
      for (const d of documentsRef.current) {
        if (d.status !== "processing" || !d.doc) continue;
        const { localId } = d;
        getDocument(d.doc.id)
          .then((doc) => {
            if (doc.status !== "processing")
              patchDocument(localId, { status: doc.status, error: doc.error, doc });
          })
          .catch((e) => patchDocument(localId, { status: "failed", error: (e as Error).message }));
      }
    }, DOCUMENT_POLL_MS);
    return () => clearInterval(timer);
  }, [documentsProcessing]);

  function toggleSidebar(open: boolean) {
    setSidebarOpen(open);
    if (!isMobile) {
      try {
        localStorage.setItem(SIDEBAR_KEY, String(open));
      } catch {}
    }
  }

  async function addFiles(files: Iterable<File>) {
    const all = Array.from(files);
    addDocuments(all.filter((f) => !isImageFile(f)));
    const images = all.filter(isImageFile);
    if (images.length === 0) return;
    if (!(info?.vision ?? true)) {
      setError("현재 모델은 이미지를 지원하지 않습니다.");
      return;
    }
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

  /** 문서를 올린다. 크기·형식·개수는 서버도 검사하지만, 미리 걸러서 바로 알려준다. */
  function addDocuments(files: File[]) {
    if (files.length === 0) return;
    const limits = info?.documents;
    if (!limits?.enabled) {
      setError("문서를 첨부할 수 없습니다 (서버에 첨부 저장소가 설정되지 않음).");
      return;
    }
    const room = limits.max_per_message - documentsRef.current.length;
    if (files.length > room) setError(`문서는 최대 ${limits.max_per_message}개까지 첨부할 수 있습니다.`);
    for (const file of files.slice(0, Math.max(room, 0))) {
      if (!limits.extensions.includes(extensionOf(file.name))) {
        setError(`지원하지 않는 파일입니다: ${file.name}`);
        continue;
      }
      if (file.size > limits.max_bytes) {
        setError(`파일이 너무 큽니다 (최대 ${formatBytes(limits.max_bytes)}): ${file.name}`);
        continue;
      }
      const localId = crypto.randomUUID();
      setDocuments((prev) => [
        ...prev,
        { localId, filename: file.name, size: file.size, status: "uploading" },
      ]);
      uploadDocument(file)
        .then((doc) => {
          // 올리는 동안 사용자가 뺐으면 서버에서도 지운다.
          if (!documentsRef.current.some((d) => d.localId === localId)) {
            void deleteDocument(doc.id);
            return;
          }
          patchDocument(localId, { status: doc.status, error: doc.error, doc });
        })
        .catch((e) => patchDocument(localId, { status: "failed", error: (e as Error).message }));
    }
  }

  function removeDocument(localId: string) {
    const target = documentsRef.current.find((d) => d.localId === localId);
    if (target?.doc) void deleteDocument(target.doc.id);
    setDocuments((prev) => prev.filter((d) => d.localId !== localId));
  }

  /**
   * 대기 메시지가 있으면 다른 대화로 이동하지 않는다 (보낼 곳이 사라진다).
   * 응답 생성 중에는 옮겨도 된다: 응답은 서버에서 끝까지 만들어지고, 돌아오면 이어서 보인다.
   */
  function navigationBlocked(): boolean {
    if (queuedRef.current) {
      setToast(NAV_BLOCKED_TEXT);
      return true;
    }
    return false;
  }

  /**
   * 응답을 멈춘다. 응답은 서버의 백그라운드 작업이라(새로고침해도 이어진다) 연결을 끊지 않고 중지 API 를 부른다.
   * 서버가 저장을 마치면 스트림에 stopped 가 오고 스트림이 닫힌다.
   */
  async function stopReply() {
    const id = conversationIdRef.current;
    if (!id) {
      stopRequestedRef.current = true; // 새 대화의 ID 를 아직 못 받았다: 받으면 바로 중지한다
      return;
    }
    stopRequestedRef.current = false;
    try {
      await stopConversation(id);
    } catch (e) {
      // 중지 요청이 실패하면(네트워크 등) 이 창이라도 기다리지 않게 연결을 끊는다.
      setToast((e as Error).message);
      abortRef.current?.abort();
    }
  }

  async function openConversation(id: string, history: HistoryMode = "push") {
    if (history !== "none" && navigationBlocked()) return;
    setError(null);
    try {
      const conv = await getConversation(id);
      detachStream();
      jumpToBottomRef.current = true;
      setPinnedId(null);
      setCreatingProject(false);
      setMemoryOpen(false);
      setProjectId(conv.project_id ?? null);
      setActiveConversation(conv.id);
      setMessages(conv.messages);
      setAttachments([]);
      discardDocuments();
      setViewerTarget(null);
      setUrl(conversationPath(conv.id), history);
      if (isMobile) setSidebarOpen(false);
      // 응답을 만드는 중이면(새로고침, 다른 창·기기) 이어서 보여준다.
      if (conv.running) void attachRun(conv.id);
    } catch (e) {
      if (history === "none") {
        // 주소로 연 대화가 없다 (지웠거나 다른 사람의 대화): 새 대화 화면으로 바꾼다.
        setToast("대화를 찾을 수 없습니다.");
        newConversation("replace");
      } else {
        setError(String(e));
      }
    }
  }

  /** 빈 화면으로 간다: 대화를 닫고 입력창을 비운다. */
  function resetToEmpty() {
    detachStream();
    setActiveConversation(null);
    setPinnedId(null);
    setMessages([]);
    setAttachments([]);
    discardDocuments();
    setInput("");
    setError(null);
    setViewerTarget(null);
    if (isMobile) setSidebarOpen(false);
  }

  function newConversation(history: HistoryMode = "push") {
    if (history !== "none" && navigationBlocked()) return;
    resetToEmpty();
    setProjectId(null);
    setCreatingProject(false);
    setMemoryOpen(false);
    setHello(greeting());
    setUrl("/", history);
  }

  /** 프로젝트 화면을 연다. 여기서 보내는 메시지는 이 프로젝트 안의 새 대화가 된다. */
  function openProject(id: string, history: HistoryMode = "push") {
    if (history !== "none" && navigationBlocked()) return;
    resetToEmpty();
    setCreatingProject(false);
    setMemoryOpen(false);
    setProjectId(id);
    setUrl(projectPath(id), history);
  }

  function startNewProject(history: HistoryMode = "push") {
    if (history !== "none" && navigationBlocked()) return;
    resetToEmpty();
    setProjectId(null);
    setMemoryOpen(false);
    setCreatingProject(true);
    setUrl(routePath({ kind: "newProject" }), history);
  }

  /** 기억 관리 화면을 연다. */
  function openMemory(history: HistoryMode = "push") {
    if (history !== "none" && navigationBlocked()) return;
    resetToEmpty();
    setProjectId(null);
    setCreatingProject(false);
    setMemoryOpen(true);
    setUrl(routePath({ kind: "memory" }), history);
  }

  function onProjectDeleted() {
    refreshProjects();
    refreshConversations();
    resetToEmpty();
    setProjectId(null);
    setHello(greeting());
    setUrl("/", "replace");
  }

  /** 주소대로 화면을 연다 (처음 열 때, 뒤로·앞으로 가기). 주소는 다시 쌓지 않는다. */
  function openRoute(route: Route) {
    if (route.kind === "conversation") void openConversation(route.id, "none");
    else if (route.kind === "project") openProject(route.id, "none");
    else if (route.kind === "newProject") startNewProject("none");
    else if (route.kind === "memory") openMemory("none");
    else newConversation("none");
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
    if (id === conversationIdRef.current) {
      // 프로젝트 안의 대화였으면 그 프로젝트 화면으로 돌아간다. 지운 대화의 주소는 기록에서 바꾼다.
      const project = projectIdRef.current;
      if (project) openProject(project, "replace");
      else newConversation("replace");
    }
    refreshConversations();
  }

  /**
   * 입력창의 메시지를 처리한다.
   * - 응답 생성 중 (이 창에서 보냈거나 이어서 보는 중): 대기열에 넣음 (1개까지, 그 이상은 거절)
   * - 그 외: 바로 전송
   */
  function submit() {
    const text = input.trim();
    if (!text && attachments.length === 0 && documents.length === 0) return;
    if (documents.some((d) => d.status !== "ready" || !d.doc)) return; // 입력창이 버튼을 막는다
    if (queuedRef.current) {
      setToast(streaming ? QUEUE_FULL_TEXT : QUEUE_PENDING_TEXT);
      return;
    }
    const message: QueuedMessage = {
      id: crypto.randomUUID(),
      text,
      attachments,
      documents: documents.map((d) => d.doc as DocumentInfo),
      model: selectedModel,
    };
    setInput("");
    setAttachments([]);
    setDocuments(() => []); // 보내는 문서이므로 서버에서 지우지 않는다
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
    const { text, attachments: sending, documents: sendingDocs, model } = message;
    setError(null);
    setStreaming(true);

    const userId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    setMessages((prev) => [
      ...prev,
      {
        id: userId,
        role: "user",
        content: text,
        images: sending.map((a) => a.previewUrl),
        documents: sendingDocs,
      },
      { id: assistantId, role: "assistant", content: "", model },
    ]);

    setActiveAssistantId(assistantId);
    setPinnedId(userId);
    scrollToPinnedRef.current = true;

    stopRequestedRef.current = false;
    const isNew = conversationIdRef.current === null;
    const outcome = await followReply(
      assistantId,
      async (handlers, signal) => {
        await streamChat(
          {
            conversation_id: conversationIdRef.current,
            // 새 대화면 지금 보고 있는 프로젝트 안에 만든다 (이어가는 대화는 원래 프로젝트를 따른다).
            project_id: conversationIdRef.current === null ? projectIdRef.current : null,
            message: text,
            images: sending.map((a) => ({ mime_type: a.mimeType, data: a.base64 })),
            document_ids: sendingDocs.map((d) => d.id),
            model,
          },
          handlers,
          signal,
        );
        return true;
      },
      (state) => ({
        onMeta: ({ conversation_id }) => {
          setActiveConversation(conversation_id);
          // 빈 화면이 이 대화가 됐다: 뒤로 가기에 빈 화면이 끼지 않게 주소를 바꾼다.
          setUrl(conversationPath(conversation_id), "replace");
          if (stopRequestedRef.current) void stopReply();
        },
        onBusy: (detail) => {
          // 다른 창에서 생성 중이라 거절됨: 메시지는 저장되지 않았으니 입력창으로 되돌리고, 그 응답을 이어서 보여준다.
          state.outcome = "busy";
          setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== assistantId));
          setInput((prev) => prev || text);
          setAttachments((prev) => (prev.length ? prev : sending));
          setDocuments((prev) =>
            prev.length
              ? prev
              : sendingDocs.map((doc) => ({
                  localId: crypto.randomUUID(),
                  filename: doc.filename,
                  size: doc.size_bytes,
                  status: "ready",
                  doc,
                })),
          );
          setToast(detail || REMOTE_BUSY_TEXT);
          const id = conversationIdRef.current;
          if (id) void showRunningReply(id);
        },
      }),
    );
    if (isNew) refreshConversations();

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

  /** 응답에 👍/👎 피드백을 단다. 화면을 먼저 바꾸고, 저장에 실패하면 되돌린다. */
  async function sendFeedback(message: ChatMessage, value: 1 | -1 | null, comment: string | null = null) {
    const id = conversationIdRef.current;
    if (!id || !message.serverId) return;
    const before = { feedback: message.feedback ?? null, feedbackComment: message.feedbackComment ?? null };
    const patch = (p: Partial<ChatMessage>) =>
      setMessages((prev) => prev.map((m) => (m.id === message.id ? { ...m, ...p } : m)));
    patch({ feedback: value, feedbackComment: value === null ? null : comment });
    try {
      const saved = await setFeedback(id, message.serverId, value, comment);
      patch({ feedback: saved.feedback, feedbackComment: saved.comment });
    } catch (e) {
      patch(before);
      setToast((e as Error).message);
    }
  }

  /** 이 창의 응답 연결만 끊는다 (다른 화면으로 옮김). 응답은 서버에서 계속 만들어지고, 돌아오면 이어서 보인다. */
  function detachStream() {
    const controller = abortRef.current;
    if (!controller) return;
    detachedRef.current.add(controller);
    controller.abort();
    abortRef.current = null;
    setActiveAssistantId(null);
    setStreaming(false);
  }

  /** 응답 하나를 assistant 메시지에 그리는 핸들러 (답변, 추론, 도구, 끝 이벤트, 이벤트 번호). */
  function replyHandlers(assistantId: string, state: ReplyState): StreamHandlers {
    const updateAssistant = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? fn(m) : m)));
    const appendToAssistant = (chunk: string) =>
      updateAssistant((m) => ({ ...m, content: m.content + chunk, thinking: false }));
    return {
      onEventId: (id) => {
        state.lastId = id;
      },
      onDelta: appendToAssistant,
      // 연속된 추론 조각은 마지막 추론 단계에 이어 붙인다.
      onReasoning: (chunk) =>
        updateAssistant((m) => {
          const steps = m.steps ?? [];
          const last = steps.at(-1);
          const merged: Step[] =
            last?.type === "reasoning"
              ? [...steps.slice(0, -1), { ...last, text: last.text + chunk }]
              : [...steps, { type: "reasoning", text: chunk }];
          return { ...m, steps: merged, thinking: true };
        }),
      onToolStart: (tool) =>
        updateAssistant((m) => ({
          ...m,
          thinking: false,
          steps: [...(m.steps ?? []), { type: "tool", ...tool, output: null, status: "running" }],
        })),
      onToolEnd: ({ id, output, status, sources }) =>
        updateAssistant((m) => ({
          ...m,
          steps: (m.steps ?? []).map((st) =>
            st.type === "tool" && st.id === id ? { ...st, output, status, sources } : st,
          ),
        })),
      onDone: ({ stop_reason, model, message_id }) => {
        state.outcome = "done";
        // 저장된 메시지 ID: 이 응답에 피드백을 보낼 때 쓴다
        updateAssistant((m) => ({ ...m, model: m.model ?? model, serverId: message_id ?? null }));
        if (stop_reason === "refusal") appendToAssistant("\n\n_(요청이 거절되었습니다.)_");
        if (stop_reason === "max_tokens") appendToAssistant("\n\n_(응답이 길어 잘렸습니다.)_");
      },
      onError: (msg, { transient }) => {
        state.outcome = "error";
        // 서버 혼잡처럼 잠시 후 다시 보내면 되는 오류는 알림으로 짧게 보여준다.
        if (transient) setToast(msg);
        else setError(msg);
      },
      onStopped: () => {
        state.outcome = "aborted";
      },
    };
  }

  /**
   * 응답 하나를 assistant 메시지(`assistantId`)에 받아 그린다 (보낸 메시지의 응답, 이어서 보는 응답 모두).
   *
   * 응답은 서버의 백그라운드 작업이라, 연결이 끊겨 끝 이벤트를 못 받으면 받은 이벤트 번호 다음부터 다시 붙는다.
   * 이어 받을 실행이 없으면(끝난 지 오래됨, 서버 재시작) 서버가 저장한 결과로 바꾼다.
   *
   * @param open 첫 연결. 붙을 실행이 없으면 false.
   * @param extra 추가 핸들러 (보낸 메시지의 meta, busy 등).
   */
  async function followReply(
    assistantId: string,
    open: (handlers: StreamHandlers, signal: AbortSignal) => Promise<boolean>,
    extra: (state: ReplyState) => Partial<StreamHandlers> = () => ({}),
  ): Promise<Outcome> {
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setActiveAssistantId(assistantId);
    const state: ReplyState = { outcome: "ended", lastId: 0 };
    const handlers: StreamHandlers = { ...replyHandlers(assistantId, state), ...extra(state) };

    const attempt = async (connect: () => Promise<boolean>) => {
      state.outcome = "ended";
      try {
        if (!(await connect())) state.outcome = "gone";
      } catch (e) {
        if ((e as Error).name === "AbortError") {
          state.outcome = detachedRef.current.has(controller) ? "detached" : "aborted";
        } else if (conversationIdRef.current && state.lastId > 0) {
          state.outcome = "ended"; // 받던 중 연결이 끊김: 이어 받는다
        } else {
          state.outcome = "error";
          setError(String(e));
        }
      }
    };

    await attempt(() => open(handlers, controller.signal));
    const id = conversationIdRef.current;
    for (let n = 0; state.outcome === "ended" && id && n < RESUME_RETRIES; n++) {
      await new Promise((r) => setTimeout(r, RESUME_DELAY_MS * 2 ** n));
      if (controller.signal.aborted || conversationIdRef.current !== id) break;
      await attempt(() => resumeStream(id, state.lastId, handlers, controller.signal));
    }

    if ((state.outcome === "ended" || state.outcome === "gone") && id && conversationIdRef.current === id) {
      // 끝을 받지 못했다: 서버가 저장한 결과(끝까지 만든 응답, 또는 중단으로 저장된 응답)로 바꾼다.
      if (!(await reloadMessages(id))) finishAssistantMessage(assistantId, "ended");
    } else if (state.outcome !== "busy" && state.outcome !== "detached") {
      finishAssistantMessage(assistantId, state.outcome);
    }
    // 그 사이 다른 응답을 받기 시작했으면(다른 화면으로 옮김) 그쪽 상태를 건드리지 않는다.
    if (abortRef.current === controller) {
      abortRef.current = null;
      setActiveAssistantId(null);
      setStreaming(false);
    }
    return state.outcome;
  }

  /** 서버에서 만들고 있는 응답을 이어서 보여준다 (대화를 열었을 때, 다른 창에서 생성 중일 때). */
  async function attachRun(id: string) {
    const assistantId = crypto.randomUUID();
    setMessages((prev) => [...prev, { id: assistantId, role: "assistant", content: "" }]);
    jumpToBottomRef.current = true;
    stopRequestedRef.current = false;
    await followReply(assistantId, (handlers, signal) => resumeStream(id, 0, handlers, signal));
  }

  /** 다른 창에서 보낸 메시지까지 다시 읽고, 그 응답을 이어서 보여준다 (보내려다 거절됐을 때). */
  async function showRunningReply(id: string) {
    try {
      const conv = await getConversation(id);
      if (conversationIdRef.current !== id) return;
      jumpToBottomRef.current = true;
      setMessages(conv.messages);
      if (conv.running) void attachRun(id);
    } catch (e) {
      setError(String(e));
    }
  }

  /** 서버가 저장한 메시지로 화면을 바꾼다. 실패하면 false. */
  async function reloadMessages(id: string): Promise<boolean> {
    try {
      const conv = await getConversation(id);
      if (conversationIdRef.current === id) {
        jumpToBottomRef.current = true;
        setMessages(conv.messages);
      }
      return true;
    } catch {
      setToast("응답을 다시 불러오지 못했습니다. 새로고침해 주세요.");
      return false;
    }
  }

  /**
   * 정상 종료가 아닌 직후, 서버가 이전 응답을 정리할 때까지 기다린다 (대기 메시지를 보내기 전).
   * 중지 API 는 정리가 끝난 뒤 돌려주므로 보통 바로 끝나지만, 이어 받기에 실패했을 때 등을 위해 확인한다.
   * 정리 전에 보내면 "다른 창의 요청"으로 거절되고 저장 순서도 뒤바뀐다.
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
  function finishAssistantMessage(id: string, outcome: Exclude<Outcome, "busy" | "detached">) {
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
            // 보여준 것이 없으면 자리를 지운다 (오류 내용은 오류 상자나 알림에 표시됨).
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
  // 마지막으로 보낸 메시지 앞(earlier)과 그 메시지부터(pinned). 보낸 메시지가 없으면 모두 earlier.
  const pinIndex = pinnedId ? messages.findIndex((m) => m.id === pinnedId) : -1;
  const earlier = pinIndex < 0 ? messages : messages.slice(0, pinIndex);
  const pinned = pinIndex < 0 ? [] : messages.slice(pinIndex);
  const modelLabels = Object.fromEntries(models.map((m) => [m.id, m.label]));
  const activeTitle = conversations.find((c) => c.id === conversationId)?.title;
  const activeProject = projects.find((p) => p.id === projectId);
  // 프로젝트 화면(프로젝트를 열었고 아직 대화를 시작하지 않음)
  const projectHome = isEmpty && !creatingProject && !memoryOpen && projectId !== null;

  const composer = (
    <Composer
      value={input}
      onChange={setInput}
      attachments={attachments}
      onAddFiles={addFiles}
      onRemoveAttachment={(id) => setAttachments((prev) => prev.filter((a) => a.id !== id))}
      documents={documents}
      onRemoveDocument={removeDocument}
      documentsEnabled={info?.documents?.enabled ?? false}
      documentExtensions={info?.documents?.extensions ?? []}
      onSend={submit}
      onStop={() => void stopReply()}
      streaming={streaming}
      queueFull={queued !== null}
      modelPicker={
        <ModelPicker
          models={models}
          value={selectedModel}
          onChange={chooseModel}
          onOpen={refreshModels}
          loading={modelsLoading}
        />
      }
      visionEnabled={info?.vision ?? true}
      autoFocus={isEmpty}
      placeholder={
        projectHome
          ? "이 프로젝트에 대해 물어보세요"
          : isEmpty
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
    <OpenDocumentProvider value={openDocument}>
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
            projects={projects}
            activeProjectId={projectHome ? projectId : null}
            onNewProject={() => startNewProject()}
            onOpenProject={(id) => openProject(id)}
            info={info}
            modelLabel={models.find((m) => m.id === selectedModel)?.label}
            onNew={() => newConversation()}
            onOpen={(id) => void openConversation(id)}
            onDelete={removeConversation}
            onClose={() => toggleSidebar(false)}
            user={user}
            authRequired={authRequired}
            onLogout={onLogout}
            memoryActive={memoryOpen}
            onOpenMemory={() => openMemory()}
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
            if (projectHome && projectDropRef.current) projectDropRef.current(e.dataTransfer.files);
            else void addFiles(e.dataTransfer.files);
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
                  onClick={() => newConversation()}
                  className="rounded-lg p-1.5 text-muted hover:bg-hover hover:text-fg"
                  aria-label="새 대화"
                  title={`새 대화 (${shortcutLabel("N")})`}
                >
                  <PlusIcon />
                </button>
              </>
            )}
            {!isEmpty && activeTitle && (
              <h1 className="ml-1 flex min-w-0 items-center gap-1.5 text-sm font-medium text-muted">
                {activeProject && (
                  <>
                    <button
                      onClick={() => openProject(activeProject.id)}
                      className="shrink-0 truncate rounded-md px-1 py-0.5 hover:bg-hover hover:text-fg"
                      title="프로젝트로 돌아가기"
                    >
                      {activeProject.name}
                    </button>
                    <span className="text-subtle">/</span>
                  </>
                )}
                <span className="truncate">{activeTitle}</span>
              </h1>
            )}
          </header>

          {dragging && (
            <div className="pointer-events-none absolute inset-3 z-20 flex items-center justify-center rounded-2xl border-2 border-dashed border-line-strong bg-app/85 text-muted">
              {projectHome ? "프로젝트에 문서를 올리려면 여기에 놓으세요" : "파일을 여기에 놓으세요"}
            </div>
          )}

          {memoryOpen ? (
            <MemoryView onToast={setToast} />
          ) : creatingProject ? (
            <NewProjectForm
              onCreated={(project) => {
                refreshProjects();
                openProject(project.id, "replace"); // /p/new 를 만든 프로젝트 주소로 바꾼다
              }}
              onCancel={() => newConversation()}
            />
          ) : projectHome ? (
            <ProjectView
              key={projectId}
              projectId={projectId}
              composer={
                <>
                  {errorBox}
                  {composer}
                </>
              }
              documentExtensions={info?.documents?.extensions ?? []}
              onOpenConversation={(id) => void openConversation(id)}
              onChanged={refreshProjects}
              onDeleted={onProjectDeleted}
              onToast={setToast}
              registerDrop={registerProjectDrop}
            />
          ) : isEmpty ? (
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
                  이미지와 문서를 붙여넣거나 끌어다 놓을 수 있어요 · Shift+Enter 로 줄바꿈
                </p>
              </div>
            </div>
          ) : (
            /* 대화 화면: 메시지 목록 + 하단 고정 입력창 */
            <>
              <div ref={setScrollBox} className="flex-1 overflow-y-auto">
                <div className="mx-auto w-full max-w-3xl px-4 pb-6 pt-2">
                  {earlier.length > 0 && (
                    <MessageList
                      messages={earlier}
                      activeId={activeAssistantId}
                      modelLabels={modelLabels}
                      onFeedback={sendFeedback}
                    />
                  )}
                  {/* 마지막으로 보낸 메시지부터: 최소 화면 높이만큼 차지해서 그 메시지를 맨 위로 올릴 수 있게 한다 */}
                  <div
                    ref={pinnedTurnRef}
                    className={earlier.length > 0 && pinned.length > 0 ? "mt-7" : undefined}
                    style={
                      pinned.length > 0
                        ? { minHeight: Math.max(0, scrollBoxHeight - PIN_GAP_PX - PIN_BOTTOM_PX) }
                        : undefined
                    }
                  >
                    <MessageList
                      messages={pinned}
                      activeId={activeAssistantId}
                      modelLabels={modelLabels}
                      onFeedback={sendFeedback}
                    />
                    {queued && (
                      <QueuedMessageBubble
                        message={queued}
                        paused={queuePaused}
                        onSendNow={sendQueuedNow}
                        onRemove={() => setQueued(null)}
                      />
                    )}
                    </div>
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
        {viewerTarget && <DocumentViewer target={viewerTarget} onClose={closeViewer} />}
        <Toast message={toast} onClose={closeToast} />
      </div>
    </OpenDocumentProvider>
  );
}
