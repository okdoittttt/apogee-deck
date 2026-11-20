"use client";

import { useCallback, useEffect, useState } from "react";
import { AuthGate, Avatar } from "@/components/Auth";
import { LogoIcon } from "@/components/icons";
import { type AdminUser, type Decision, decideUser, listUsers } from "@/lib/auth";

const STATUS_LABELS: Record<AdminUser["status"], string> = {
  pending: "승인 대기",
  approved: "승인됨",
  rejected: "거절됨",
  suspended: "정지됨",
};

const STATUS_STYLES: Record<AdminUser["status"], string> = {
  pending: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  approved: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  rejected: "bg-red-500/15 text-red-700 dark:text-red-300",
  suspended: "bg-zinc-500/20 text-muted",
};

/** 상태별로 할 수 있는 처리. 관리자 자신은 거절·정지할 수 없다 (서버도 막는다). */
function actionsFor(user: AdminUser): { decision: Decision; label: string; danger?: boolean }[] {
  switch (user.status) {
    case "pending":
      return [
        { decision: "approve", label: "승인" },
        { decision: "reject", label: "거절", danger: true },
      ];
    case "approved":
      return user.is_admin ? [] : [{ decision: "suspend", label: "정지", danger: true }];
    case "rejected":
      return [{ decision: "approve", label: "승인" }];
    case "suspended":
      return [{ decision: "reactivate", label: "정지 해제" }];
  }
}

const CONFIRM_TEXT: Partial<Record<Decision, string>> = {
  reject: "가입 신청을 거절할까요? 신청자에게 결과 메일이 갑니다.",
  suspend: "이 사용자를 정지할까요? 바로 로그아웃되고 쓸 수 없게 됩니다.",
};

function formatDate(value: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
}

function AdminPanel() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    listUsers()
      .then((list) => {
        setUsers(list);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(load, [load]);

  async function decide(user: AdminUser, decision: Decision) {
    const confirmText = CONFIRM_TEXT[decision];
    if (confirmText && !window.confirm(`${user.name} (${user.email})\n\n${confirmText}`)) return;
    setBusy(user.id);
    try {
      const updated = await decideUser(user.id, decision);
      setUsers((prev) => prev?.map((u) => (u.id === updated.id ? updated : u)) ?? null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const pendingCount = users?.filter((u) => u.status === "pending").length ?? 0;

  return (
    <div className="min-h-dvh bg-app text-fg">
      <header className="mx-auto flex max-w-3xl items-center justify-between px-4 py-5">
        <div className="flex items-center gap-2.5">
          <LogoIcon className="h-7 w-7" />
          <h1 className="text-lg font-semibold">사용자 관리</h1>
          {pendingCount > 0 && (
            <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-300">
              승인 대기 {pendingCount}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-hover"
          >
            새로고침
          </button>
          <a href="/" className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-hover">
            채팅으로
          </a>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 pb-10">
        {error && (
          <div className="mb-4 flex items-start justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-600 dark:text-red-300">
            <span>{error}</span>
            <button onClick={() => setError(null)} className="opacity-70 hover:opacity-100">
              닫기
            </button>
          </div>
        )}
        {users === null ? (
          <p className="text-sm text-muted">불러오는 중…</p>
        ) : users.length === 0 ? (
          <p className="text-sm text-muted">아직 로그인한 사용자가 없습니다.</p>
        ) : (
          <ul className="space-y-3">
            {users.map((u) => (
              <li key={u.id} className="rounded-2xl border border-line bg-surface p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <Avatar user={u} size="h-9 w-9" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium">{u.name}</span>
                        <span className={`rounded-full px-2 py-0.5 text-[11px] ${STATUS_STYLES[u.status]}`}>
                          {STATUS_LABELS[u.status]}
                        </span>
                        {u.is_admin && <span className="text-[11px] text-muted">관리자</span>}
                      </div>
                      <div className="truncate text-xs text-muted">{u.email}</div>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    {actionsFor(u).map((a) => (
                      <button
                        key={a.decision}
                        onClick={() => decide(u, a.decision)}
                        disabled={busy === u.id}
                        className={`rounded-lg px-3 py-1.5 text-sm disabled:opacity-40 ${
                          a.danger
                            ? "border border-red-500/40 text-red-600 hover:bg-red-500/10 dark:text-red-300"
                            : "bg-fg text-app hover:opacity-85"
                        }`}
                      >
                        {a.label}
                      </button>
                    ))}
                  </div>
                </div>
                {u.request_reason && (
                  <p className="mt-3 whitespace-pre-wrap break-words rounded-xl bg-hover px-3 py-2 text-sm">
                    {u.request_reason}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-subtle">
                  <span>가입 {formatDate(u.created_at)}</span>
                  <span>신청 {formatDate(u.requested_at)}</span>
                  <span>마지막 로그인 {formatDate(u.last_login_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}

export default function AdminPage() {
  return <AuthGate requireAdmin>{() => <AdminPanel />}</AuthGate>;
}
