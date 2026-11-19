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

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  images?: string[]; // 표시용 URL (서버 이미지 URL 또는 data URL)
  steps?: Step[]; // assistant 응답의 실행 단계 (일어난 순서)
  thinking?: boolean; // 화면 전용: 지금 추론 과정을 받는 중인지
}

interface MessageOut {
  id: string;
  role: Role;
  content: string;
  image_ids: string[];
  steps: Step[];
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
  body: { conversation_id: string | null; message: string; images?: ImageInput[] },
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

export interface AppInfo {
  provider: string;
  model: string;
  vision: boolean;
}

export async function getInfo(): Promise<AppInfo> {
  const res = await fetch(`${API_URL}/api/info`);
  if (!res.ok) throw new Error(`Failed to load info (${res.status})`);
  return res.json();
}
