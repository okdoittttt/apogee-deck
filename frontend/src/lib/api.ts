export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** 로그인 상태가 바뀌었을 수 있을 때(401, 승인 상태 403) 보내는 이벤트. AuthGate 가 내 정보를 다시 읽는다. */
export const AUTH_CHANGED_EVENT = "auth-changed";

/**
 * 백엔드 요청. 세션 쿠키를 붙이고(화면과 백엔드의 포트가 달라도), 로그인이 풀렸거나
 * 승인 상태가 바뀌어 거절되면 AUTH_CHANGED_EVENT 를 보낸다.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${API_URL}${path}`, { credentials: "include", ...init });
  if (res.status === 401 || (res.status === 403 && (await isStatusError(res)))) {
    window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
  }
  return res;
}

/** 403 이 승인 상태(대기·거절·정지) 때문인지. 본문은 복사본으로 읽어서 호출한 쪽이 다시 읽을 수 있다. */
async function isStatusError(res: Response): Promise<boolean> {
  try {
    const body = await res.clone().json();
    return typeof body?.detail === "object" && typeof body.detail?.code === "string";
  } catch {
    return false;
  }
}

export type Role = "user" | "assistant";

/** 실행 단계: 모델의 추론 과정 (Claude 는 요약, qwen 등은 원문). */
export interface ReasoningStep {
  type: "reasoning";
  text: string;
}

/** 도구가 참고한 웹 페이지 (웹 검색 결과). 답변 아래 출처 목록에 보여준다. */
export interface WebSource {
  type?: undefined;
  title: string;
  url: string;
  published_date: string | null;
}

/** 도구가 참고한 프로젝트 문서 위치 (프로젝트 문서 검색 결과). */
export interface DocumentSource {
  type: "document";
  title: string; // 문서 이름
  document_id: string;
  project_id: string;
  location: string; // 쪽과 제목 경로 (예: "p.3 · 1장 > 1.1"), 없으면 빈 문자열
  image_element_id: string | null; // 그림 결과면 그림 요소 ID (미리보기)
}

export type Source = WebSource | DocumentSource;

/** 실행 단계: 도구 호출 하나. */
export interface ToolStep {
  type: "tool";
  id: string;
  name: string;
  label: string; // 실행 중 문구 (예: "시간을 확인하는 중")
  done_label: string; // 완료 문구 (예: "시간 확인함")
  args: Record<string, unknown>;
  output: string | null;
  status: "running" | "done" | "error" | "interrupted";
  sources?: Source[];
}

export type Step = ReasoningStep | ToolStep;

/** 첨부 문서 정보와 파싱 상태 (본문 제외). */
export interface DocumentInfo {
  id: string;
  filename: string;
  kind: string; // pdf, docx, hwpx ...
  mime_type: string;
  size_bytes: number;
  // analyzing: 프로젝트 문서의 그림 설명 중, indexing: 검색 준비(청크·임베딩) 중
  status: "processing" | "analyzing" | "indexing" | "ready" | "failed";
  error: string | null;
  page_count: number | null;
  char_count: number | null;
  token_count?: number | null; // 프로젝트 문서만: 본문과 그림 설명의 추정 토큰 수 (용량 계산)
  image_count?: number | null; // 프로젝트 문서만: 설명할 그림 수
  image_fallback_count?: number | null; // 프로젝트 문서만: 설명을 만들지 못해 OCR 글자로 대신한 그림 수
  created_at: string;
  parsed_at: string | null;
}

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  images?: string[]; // 표시용 URL (서버 이미지 URL 또는 data URL)
  steps?: Step[]; // assistant 응답의 실행 단계 (일어난 순서)
  thinking?: boolean; // 화면 전용: 지금 추론 과정을 받는 중인지
  model?: string | null; // assistant 응답을 만든 모델 ID
  documents?: DocumentInfo[]; // 사용자 메시지에 첨부한 문서
}

interface MessageOut {
  id: string;
  role: Role;
  content: string;
  image_ids: string[];
  steps: Step[];
  model: string | null;
  documents: DocumentInfo[];
}

export const imageUrl = (id: string) => `${API_URL}/api/images/${id}`;
/** 프로젝트 문서 속 그림 원본 주소 (검색 결과 미리보기). */
export const projectImageUrl = (projectId: string, elementId: string) =>
  `${API_URL}/api/projects/${projectId}/images/${elementId}`;

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  project_id?: string | null; // 속한 프로젝트 (프로젝트 밖의 대화는 null)
}

export async function listConversations(): Promise<Conversation[]> {
  const res = await apiFetch(`/api/conversations`);
  if (!res.ok) throw new Error(`Failed to load conversations (${res.status})`);
  return res.json();
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  running: boolean; // 이 대화의 응답을 생성 중인지 (다른 창일 수 있음)
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const res = await apiFetch(`/api/conversations/${id}`);
  if (!res.ok) throw new Error(`Failed to load conversation (${res.status})`);
  const data: Conversation & { messages: MessageOut[]; running: boolean } = await res.json();
  return {
    ...data,
    messages: data.messages.map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      images: m.image_ids.map(imageUrl),
      steps: m.steps,
      model: m.model,
      documents: m.documents ?? [],
    })),
  };
}

export interface ImageInput {
  mime_type: string;
  data: string; // base64
}

/** 대화의 응답 생성 여부. 다른 창의 응답이 끝났는지 주기적으로 확인할 때 쓴다. */
export async function getConversationStatus(id: string): Promise<{ running: boolean }> {
  const res = await apiFetch(`/api/conversations/${id}/status`);
  if (!res.ok) throw new Error(`Failed to load status (${res.status})`);
  return res.json();
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const err = await res.json();
    return typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
  } catch {
    return "";
  }
}

/** 대화를 삭제한다. 응답 생성 중이면 서버가 409 로 거절하며, 그 이유를 담아 예외를 던진다. */
export async function deleteConversation(id: string): Promise<void> {
  const res = await apiFetch(`/api/conversations/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error((await errorDetail(res)) || `삭제 실패 (${res.status})`);
}

// 응답이 끝까지 오지 못했을 때 붙이는 표시. 백엔드(app/conversations/turns.py)가 저장하는 문구와 같다.
export const INTERRUPTED_SUFFIX = "\n\n_(응답이 중단되었습니다.)_";
export const ERROR_SUFFIX = "\n\n_(오류로 응답이 중단되었습니다.)_";
export const NO_REPLY_TEXT = "_(응답 없음)_";

export interface ToolStartEvent {
  id: string;
  name: string;
  label: string;
  done_label: string;
  args: Record<string, unknown>;
}

export interface ToolEndEvent {
  id: string;
  output: string;
  status: "done" | "error";
  sources?: Source[];
}

export interface StreamHandlers {
  onMeta?: (data: { conversation_id: string }) => void;
  onDelta?: (text: string) => void;
  onReasoning?: (text: string) => void;
  onToolStart?: (data: ToolStartEvent) => void;
  onToolEnd?: (data: ToolEndEvent) => void;
  onDone?: (data: { stop_reason: string | null; model: string }) => void;
  /**
   * 응답 실패. `transient` 는 서버 혼잡(5xx)·사용 한도·시간 초과처럼
   * 잠시 후 다시 보내면 풀릴 수 있는 오류인지 나타낸다.
   */
  onError?: (message: string, info: { transient: boolean }) => void;
  /** 같은 대화의 응답이 다른 창에서 생성 중이라 거절됨 (409). 메시지는 저장되지 않았다. */
  onBusy?: (detail: string) => void;
}

/** POST /api/chat 의 SSE 응답을 읽어 이벤트별 핸들러를 호출한다. */
export async function streamChat(
  body: {
    conversation_id: string | null;
    project_id?: string | null; // 새 대화를 만들 프로젝트 (이어가는 대화에서는 무시됨)
    message: string;
    images?: ImageInput[];
    document_ids?: string[];
    model?: string | null;
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await apiFetch(`/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await errorDetail(res);
    if (res.status === 409) {
      handlers.onBusy?.(detail);
      return;
    }
    handlers.onError?.(`요청 실패 (${res.status})${detail ? `: ${detail}` : ""}`, {
      transient: false,
    });
    return;
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      let event = "message";
      let data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      const payload = JSON.parse(data);

      switch (event) {
        case "meta":
          handlers.onMeta?.(payload);
          break;
        case "delta":
          handlers.onDelta?.(payload.text);
          break;
        case "reasoning":
          handlers.onReasoning?.(payload.text);
          break;
        case "tool_start":
          handlers.onToolStart?.(payload);
          break;
        case "tool_end":
          handlers.onToolEnd?.(payload);
          break;
        case "done":
          handlers.onDone?.(payload);
          break;
        case "error":
          handlers.onError?.(payload.message, { transient: payload.transient === true });
          break;
      }
    }
  }
}

export interface DocumentLimits {
  enabled: boolean; // 첨부 저장소를 쓸 수 있는지
  max_bytes: number;
  max_per_message: number;
  extensions: string[]; // 받는 확장자 (점 없이)
}

export interface AppInfo {
  provider: string;
  model: string;
  vision: boolean;
  documents?: DocumentLimits;
}

// --- 첨부 문서 ---

export const documentFileUrl = (id: string) => `${API_URL}/api/documents/${id}/file`;

/** 문서를 올린다. 서버는 바로 processing 상태로 응답하고 백그라운드에서 파싱한다. */
export async function uploadDocument(file: File, signal?: AbortSignal): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch(`/api/documents`, { method: "POST", body: form, signal });
  if (!res.ok) throw new Error((await errorDetail(res)) || `업로드 실패 (${res.status})`);
  return res.json();
}

/** 문서의 파싱 상태를 확인한다. */
export async function getDocument(id: string): Promise<DocumentInfo> {
  const res = await apiFetch(`/api/documents/${id}`);
  if (!res.ok) throw new Error((await errorDetail(res)) || `문서 조회 실패 (${res.status})`);
  return res.json();
}

/** 보내지 않은 문서를 지운다 (입력창에서 첨부를 뺄 때). 실패해도 서버가 하루 뒤 지운다. */
export async function deleteDocument(id: string): Promise<void> {
  await apiFetch(`/api/documents/${id}`, { method: "DELETE" }).catch(() => undefined);
}

export async function getInfo(): Promise<AppInfo> {
  const res = await apiFetch(`/api/info`);
  if (!res.ok) throw new Error(`Failed to load info (${res.status})`);
  return res.json();
}

/** 화면에서 고를 수 있는 모델. id 를 채팅 요청의 model 로 보낸다. */
export interface ModelOption {
  id: string; // "<provider>:<모델 이름>"
  provider: string;
  provider_label: string;
  model: string;
  label: string;
}

export interface ModelList {
  models: ModelOption[];
  default: string | null;
}

/** 지금 쓸 수 있는 모델 목록 (API 키가 있는 provider, 실행 중인 Ollama 의 설치 모델). */
export async function getModels(): Promise<ModelList> {
  const res = await apiFetch(`/api/models`);
  if (!res.ok) throw new Error(`Failed to load models (${res.status})`);
  return res.json();
}

// --- 프로젝트 ---

/** 프로젝트: 문서 묶음과 지시문, 그 안의 대화들. */
export interface Project {
  id: string;
  name: string;
  instructions: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  documents: DocumentInfo[];
  conversations: Conversation[];
  used_tokens: number; // 준비된 문서의 추정 토큰 수 합계
  max_tokens: number; // 용량 상한
}

async function json<T>(res: Response, fallback: string): Promise<T> {
  if (!res.ok) throw new Error((await errorDetail(res)) || `${fallback} (${res.status})`);
  return res.json();
}

export async function listProjects(): Promise<Project[]> {
  return json(await apiFetch(`/api/projects`), "프로젝트 목록을 불러오지 못했습니다");
}

export async function getProject(id: string): Promise<ProjectDetail> {
  return json(await apiFetch(`/api/projects/${id}`), "프로젝트를 불러오지 못했습니다");
}

export async function createProject(body: { name: string; instructions: string }): Promise<Project> {
  const res = await apiFetch(`/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return json(res, "프로젝트를 만들지 못했습니다");
}

/** 보낸 값만 바꾼다. */
export async function updateProject(
  id: string,
  body: { name?: string; instructions?: string },
): Promise<Project> {
  const res = await apiFetch(`/api/projects/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return json(res, "프로젝트를 저장하지 못했습니다");
}

/** 프로젝트와 그 안의 대화·문서를 모두 지운다. 응답 생성 중인 대화가 있으면 서버가 거절한다. */
export async function deleteProject(id: string): Promise<void> {
  const res = await apiFetch(`/api/projects/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error((await errorDetail(res)) || `삭제 실패 (${res.status})`);
}

/** 프로젝트에 문서를 올린다. 서버는 바로 processing 상태로 응답하고 백그라운드에서 읽는다. */
export async function uploadProjectDocument(projectId: string, file: File): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch(`/api/projects/${projectId}/documents`, { method: "POST", body: form });
  return json(res, "업로드 실패");
}

export async function deleteProjectDocument(projectId: string, documentId: string): Promise<void> {
  const res = await apiFetch(`/api/projects/${projectId}/documents/${documentId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error((await errorDetail(res)) || `삭제 실패 (${res.status})`);
}
