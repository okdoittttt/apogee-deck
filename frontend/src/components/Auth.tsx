"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { AUTH_CHANGED_EVENT } from "@/lib/api";
import { type Me, type User, getMe, loginUrl, logout, requestAccess } from "@/lib/auth";
import { LogoIcon } from "./icons";

const PENDING_POLL_MS = 15000;

/**
 * 로그인·승인 상태에 따라 화면을 고른다.
 * - 로그인 안 함 → 로그인 화면
 * - 승인 대기 / 거절 / 정지 → 상태 화면
 * - 승인됨 → children(user) (requireAdmin 이면 관리자만)
 * 요청이 401·403(승인 상태)으로 거절되면 내 정보를 다시 읽는다 (세션 만료, 정지 등).
 */
export function AuthGate({
  children,
  requireAdmin = false,
}: {
  children: (user: User, me: Me, onLogout: () => void) => ReactNode;
  requireAdmin?: boolean;
}) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loginError, setLoginError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getMe()
      .then((m) => {
        setMe(m);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    // 로그인 실패 사유는 주소의 ?login_error= 로 온다. 보여준 뒤 주소에서 지운다.
    const params = new URLSearchParams(window.location.search);
    const err = params.get("login_error");
    if (err) {
      setLoginError(err);
      params.delete("login_error");
      const rest = params.toString();
      window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
    }
    refresh();
    window.addEventListener(AUTH_CHANGED_EVENT, refresh);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, refresh);
  }, [refresh]);

  const onLogout = useCallback(() => {
    void logout().finally(refresh);
  }, [refresh]);

  // 승인 대기 중이면 주기적으로 확인해서, 승인되는 즉시 화면을 연다.
  const pending = me?.user?.status === "pending";
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(refresh, PENDING_POLL_MS);
    return () => clearInterval(timer);
  }, [pending, refresh]);

  if (error) {
    return (
      <CenteredCard title="서버에 연결하지 못했습니다">
        <p className="text-sm text-muted">{error}</p>
        <button onClick={refresh} className={secondaryButton}>
          다시 시도
        </button>
      </CenteredCard>
    );
  }
  if (!me) return <div className="h-dvh bg-app" aria-busy />;
  if (!me.user) return <LoginScreen me={me} error={loginError} />;
  if (me.user.status === "pending")
    return <PendingScreen user={me.user} onLogout={onLogout} onUpdated={refresh} />;
  if (me.user.status !== "approved") return <BlockedScreen user={me.user} onLogout={onLogout} />;
  if (requireAdmin && !me.user.is_admin) {
    return (
      <CenteredCard title="관리자만 볼 수 있습니다">
        <a href="/" className={secondaryButton}>
          채팅으로 돌아가기
        </a>
      </CenteredCard>
    );
  }
  return <>{children(me.user, me, onLogout)}</>;
}

const primaryButton =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-fg px-4 py-2.5 text-sm font-medium text-app hover:opacity-85 disabled:opacity-40";
const secondaryButton =
  "inline-flex items-center justify-center rounded-xl border border-line px-4 py-2 text-sm text-fg hover:bg-hover";

function CenteredCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex h-dvh items-center justify-center bg-app px-4 text-fg">
      <div className="w-full max-w-[420px] rounded-2xl border border-line bg-surface p-7 shadow-sm">
        <div className="mb-5 flex items-center gap-2.5">
          <LogoIcon className="h-7 w-7" />
          <span className="text-[17px] font-semibold tracking-tight">okdoitttt</span>
        </div>
        <h1 className="mb-3 text-lg font-semibold">{title}</h1>
        <div className="space-y-4">{children}</div>
      </div>
    </div>
  );
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden>
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.4h6.5a5.6 5.6 0 0 1-2.4 3.6v3h3.9c2.3-2.1 3.5-5.2 3.5-8.7z" />
      <path fill="#34A853" d="M12 24c3.2 0 6-1.1 8-2.9l-3.9-3c-1.1.7-2.5 1.2-4.1 1.2-3.1 0-5.8-2.1-6.7-5H1.3v3.1A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.3 14.3a7.2 7.2 0 0 1 0-4.6V6.6h-4a12 12 0 0 0 0 10.8z" />
      <path fill="#EA4335" d="M12 4.8c1.8 0 3.3.6 4.6 1.8l3.4-3.4A12 12 0 0 0 1.3 6.6l4 3.1c.9-2.9 3.6-4.9 6.7-4.9z" />
    </svg>
  );
}

function LoginScreen({ me, error }: { me: Me; error: string | null }) {
  return (
    <CenteredCard title="로그인">
      <p className="text-sm leading-relaxed text-muted">
        초대받은 사람만 쓸 수 있습니다. Google 계정으로 로그인한 뒤 가입을 신청하면, 관리자가 승인한
        뒤에 쓸 수 있어요.
      </p>
      {error && (
        <p className="rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-300">
          {error}
        </p>
      )}
      {me.google_login_enabled ? (
        <a href={loginUrl} className={`${primaryButton} w-full`}>
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-white">
            <GoogleMark />
          </span>
          Google 로 로그인
        </a>
      ) : (
        <p className="text-sm text-muted">Google 로그인이 설정되지 않았습니다. 관리자에게 문의해 주세요.</p>
      )}
    </CenteredCard>
  );
}

function UserLine({ user }: { user: User }) {
  return (
    <div className="flex items-center gap-3 rounded-xl bg-hover px-3 py-2.5">
      <Avatar user={user} />
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{user.name}</div>
        <div className="truncate text-xs text-muted">{user.email}</div>
      </div>
    </div>
  );
}

export function Avatar({ user, size = "h-8 w-8" }: { user: User; size?: string }) {
  if (user.picture) {
    // eslint-disable-next-line @next/next/no-img-element
    return (
      <img
        src={user.picture}
        alt=""
        referrerPolicy="no-referrer"
        className={`${size} shrink-0 rounded-full object-cover`}
      />
    );
  }
  return (
    <span
      className={`${size} flex shrink-0 items-center justify-center rounded-full bg-fg text-xs font-medium text-app`}
    >
      {(user.name || user.email).slice(0, 1).toUpperCase()}
    </span>
  );
}

function PendingScreen({
  user,
  onLogout,
  onUpdated,
}: {
  user: User;
  onLogout: () => void;
  onUpdated: () => void;
}) {
  const [reason, setReason] = useState(user.request_reason ?? "");
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const requested = Boolean(user.request_reason);

  async function submit() {
    setSending(true);
    setMessage(null);
    try {
      const notified = await requestAccess(reason);
      setFailed(false);
      setMessage(
        notified
          ? "신청을 보냈습니다. 승인되면 메일로 알려 드려요."
          : "신청은 저장했지만 관리자에게 알림 메일을 보내지 못했습니다. 관리자에게 직접 알려 주세요.",
      );
      onUpdated();
    } catch (e) {
      setFailed(true);
      setMessage((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <CenteredCard title={requested ? "승인을 기다리고 있어요" : "가입 신청"}>
      <UserLine user={user} />
      <p className="text-sm leading-relaxed text-muted">
        {requested
          ? "관리자가 승인하면 메일로 알려 드리고, 이 화면이 자동으로 바뀝니다. 신청 내용을 고쳐서 다시 보낼 수도 있어요."
          : "관리자가 승인해야 쓸 수 있습니다. 어떤 분인지, 어떻게 쓰실지 짧게 적어 주세요."}
      </p>
      <textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        maxLength={1000}
        rows={4}
        placeholder="예) ○○의 친구입니다. 문서 요약에 써 보고 싶어요."
        className="block w-full resize-none rounded-xl border border-line bg-app px-3 py-2.5 text-sm text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none"
      />
      {message && (
        <p className={`text-sm ${failed ? "text-red-600 dark:text-red-300" : "text-emerald-700 dark:text-emerald-300"}`}>
          {message}
        </p>
      )}
      <div className="flex items-center justify-between gap-2">
        <button onClick={onLogout} className="text-sm text-muted hover:text-fg">
          로그아웃
        </button>
        <button onClick={submit} disabled={sending} className={primaryButton}>
          {sending ? "보내는 중…" : requested ? "다시 신청하기" : "신청하기"}
        </button>
      </div>
    </CenteredCard>
  );
}

function BlockedScreen({ user, onLogout }: { user: User; onLogout: () => void }) {
  const text =
    user.status === "rejected"
      ? "가입 신청이 승인되지 않았습니다."
      : "사용이 정지된 계정입니다. 관리자에게 문의해 주세요.";
  return (
    <CenteredCard title={user.status === "rejected" ? "가입 신청 결과" : "사용할 수 없는 계정"}>
      <UserLine user={user} />
      <p className="text-sm text-muted">{text}</p>
      <button onClick={onLogout} className={secondaryButton}>
        다른 계정으로 로그인
      </button>
    </CenteredCard>
  );
}
