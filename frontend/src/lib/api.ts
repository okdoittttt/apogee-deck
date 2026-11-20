export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Role = "user" | "assistant";

/** 실행 단계: 모델의 추론 과정 (Claude 는 요약, qwen 등은 원문). */
export interface ReasoningStep {
  type: "reasoning";
  text: string;
}

/** 도구가 참고한 페이지 (웹 검색 결과 등). 답변 아래 출처 목록에 보여준다. */
export interface Source {
  title: string;
  url: string;
  published_date: string | null;
}

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
  status: "processing" | "ready" | "failed";
  error: string | null;
  page_count: number | null;
  char_count: number | null;
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

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
}

export async function listConversations(): Promise<Conversation[]> {
  const res = await fetch(`${API_URL}/api/conversations`);
  if (!res.ok) throw new Error(`Failed to load conversations (${res.status})`);
  return res.json();
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  running: boolean; // 이 대화의 응답을 생성 중인지 (다른 창일 수 있음)
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const res = await fetch(`${API_URL}/api/conversations/${id}`);
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
  const res = await fetch(`${API_URL}/api/conversations/${id}/status`);
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
  const res = await fetch(`${API_URL}/api/conversations/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error((await errorDetail(res)) || `삭제 실패 (${res.status})`);
}

// 응답이 끝까지 오지 못했을 때 붙이는 표시. 백엔드(routers/chat.py)가 저장하는 문구와 같다.
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
  onError?: (message: string) => void;
  /** 같은 대화의 응답이 다른 창에서 생성 중이라 거절됨 (409). 메시지는 저장되지 않았다. */
  onBusy?: (detail: string) => void;
}

/** POST /api/chat 의 SSE 응답을 읽어 이벤트별 핸들러를 호출한다. */
export async function streamChat(
  body: {
    conversation_id: string | null;
    message: string;
    images?: ImageInput[];
    document_ids?: string[];
    model?: string | null;
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}/api/chat`, {
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
    handlers.onError?.(`요청 실패 (${res.status})${detail ? `: ${detail}` : ""}`);
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
          handlers.onError?.(payload.message);
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
  const res = await fetch(`${API_URL}/api/documents`, { method: "POST", body: form, signal });
  if (!res.ok) throw new Error((await errorDetail(res)) || `업로드 실패 (${res.status})`);
  return res.json();
}

/** 문서의 파싱 상태를 확인한다. */
export async function getDocument(id: string): Promise<DocumentInfo> {
  const res = await fetch(`${API_URL}/api/documents/${id}`);
  if (!res.ok) throw new Error((await errorDetail(res)) || `문서 조회 실패 (${res.status})`);
  return res.json();
}

/** 보내지 않은 문서를 지운다 (입력창에서 첨부를 뺄 때). 실패해도 서버가 하루 뒤 지운다. */
export async function deleteDocument(id: string): Promise<void> {
  await fetch(`${API_URL}/api/documents/${id}`, { method: "DELETE" }).catch(() => undefined);
}

export async function getInfo(): Promise<AppInfo> {
  const res = await fetch(`${API_URL}/api/info`);
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
  const res = await fetch(`${API_URL}/api/models`);
  if (!res.ok) throw new Error(`Failed to load models (${res.status})`);
  return res.json();
}
