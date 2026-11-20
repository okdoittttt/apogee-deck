import { API_URL, apiFetch } from "./api";

export type UserStatus = "pending" | "approved" | "rejected" | "suspended";

export interface User {
  id: string;
  email: string;
  name: string;
  picture: string | null;
  status: UserStatus;
  is_admin: boolean;
  request_reason: string | null;
}

export interface Me {
  auth_required: boolean; // false 면 로그인 없이 로컬 사용자로 동작 (개발용)
  google_login_enabled: boolean;
  user: User | null;
}

export interface AdminUser extends User {
  created_at: string;
  requested_at: string | null;
  last_login_at: string | null;
}

export type Decision = "approve" | "reject" | "suspend" | "reactivate";

/** Google 로그인 시작 주소. 브라우저를 이 주소로 보내면 Google 로그인 후 화면으로 돌아온다. */
export const loginUrl = `${API_URL}/api/auth/google/login`;

async function detail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.detail?.message === "string") return body.detail.message;
  } catch {}
  return fallback;
}

/** 로그인 상태와 내 정보. 로그인하지 않았으면 user 가 null 이다. */
export async function getMe(): Promise<Me> {
  const res = await fetch(`${API_URL}/api/auth/me`, { credentials: "include" });
  if (!res.ok) throw new Error(`로그인 상태를 확인하지 못했습니다 (${res.status})`);
  return res.json();
}

export async function logout(): Promise<void> {
  await apiFetch("/api/auth/logout", { method: "POST" });
}

/** 가입을 신청한다. 관리자에게 메일이 간다. 반환값은 메일을 보냈는지. */
export async function requestAccess(reason: string): Promise<boolean> {
  const res = await apiFetch("/api/auth/request", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) throw new Error(await detail(res, `신청하지 못했습니다 (${res.status})`));
  return (await res.json()).notified;
}

export async function listUsers(): Promise<AdminUser[]> {
  const res = await apiFetch("/api/admin/users");
  if (!res.ok) throw new Error(await detail(res, `사용자 목록을 불러오지 못했습니다 (${res.status})`));
  return res.json();
}

export async function decideUser(id: string, decision: Decision): Promise<AdminUser> {
  const res = await apiFetch(`/api/admin/users/${id}/${decision}`, { method: "POST" });
  if (!res.ok) throw new Error(await detail(res, `처리하지 못했습니다 (${res.status})`));
  return res.json();
}
