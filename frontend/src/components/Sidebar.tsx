"use client";

import { conversationPath, isPlainClick, projectPath, routePath } from "@/lib/routes";
import { forwardRef, useMemo, useState } from "react";
import type { AppInfo, Conversation, Project } from "@/lib/api";
import type { User } from "@/lib/auth";
import { Avatar } from "./Auth";
import { FolderIcon, LogoIcon, PlusIcon, SearchIcon, SidebarIcon, TrashIcon } from "./icons";
import { shortcutLabel } from "@/lib/platform";

interface SidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  projects: Project[];
  /** 프로젝트 화면을 보고 있으면 그 프로젝트 ID (프로젝트 안의 대화를 보고 있을 때는 null) */
  activeProjectId: string | null;
  onNewProject: () => void;
  onOpenProject: (id: string) => void;
  info: AppInfo | null;
  /** 지금 고른 모델의 표시 이름 */
  modelLabel?: string;
  onNew: () => void;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
  /** 로그인한 사용자 (아래쪽 사용자 메뉴) */
  user?: User;
  /** 로그인을 요구하는 설정인지. 아니면(로컬 사용자) 로그아웃을 숨긴다. */
  authRequired?: boolean;
  onLogout?: () => void;
  /** 기억 관리 화면을 보고 있는지 */
  memoryActive?: boolean;
  onOpenMemory?: () => void;
}

export const Sidebar = forwardRef<HTMLInputElement, SidebarProps>(function Sidebar(
  {
    conversations,
    activeId,
    projects,
    activeProjectId,
    onNewProject,
    onOpenProject,
    info,
    modelLabel,
    onNew,
    onOpen,
    onDelete,
    onClose,
    user,
    authRequired = false,
    onLogout,
    memoryActive = false,
    onOpenMemory,
  },
  searchRef,
) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? conversations.filter((c) => c.title.toLowerCase().includes(q)) : conversations;
  }, [conversations, query]);

  return (
    <aside className="flex h-full w-[264px] shrink-0 flex-col border-r border-line bg-sidebar">
      {/* Brand */}
      <div className="flex h-14 items-center justify-between px-3">
        <div className="flex items-center gap-2 px-1 text-fg">
          <LogoIcon className="h-6 w-6" />
          <span className="text-[17px] font-semibold tracking-tight">okdoitttt</span>
        </div>
        <button
          onClick={onClose}
          className="rounded-lg p-1.5 text-muted hover:bg-hover hover:text-fg"
          aria-label="사이드바 닫기"
          title="사이드바 닫기"
        >
          <SidebarIcon />
        </button>
      </div>

      {/* Actions */}
      <div className="space-y-0.5 px-2">
        <button
          onClick={onNew}
          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm text-fg hover:bg-hover"
        >
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-fg text-app">
            <PlusIcon className="h-3.5 w-3.5" />
          </span>
          <span className="flex-1 text-left">새 대화</span>
          <kbd
            className="hidden shrink-0 font-sans text-[11px] text-subtle md:block"
            title={`${shortcutLabel("N")} 또는 ${shortcutLabel("O", { shift: true })}`}
          >
            {shortcutLabel("N")}
          </kbd>
        </button>
        <label className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm text-muted focus-within:bg-hover hover:bg-hover">
          <SearchIcon className="h-[18px] w-[18px] shrink-0" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQuery("")}
            placeholder="대화 검색"
            className="w-full bg-transparent text-fg placeholder:text-muted focus:outline-none"
          />
          <kbd className="hidden shrink-0 font-sans text-[11px] text-subtle md:block">{shortcutLabel("K")}</kbd>
        </label>
      </div>

      <nav className="mt-4 flex-1 overflow-y-auto px-2 pb-3">
        {/* Projects */}
        <div className="group/projects flex items-center justify-between px-2 pb-1">
          <span className="text-xs font-medium text-subtle">프로젝트</span>
          <button
            onClick={onNewProject}
            className="rounded-md p-0.5 text-subtle hover:bg-hover hover:text-fg"
            aria-label="새 프로젝트"
            title="새 프로젝트"
          >
            <PlusIcon className="h-3.5 w-3.5" />
          </button>
        </div>
        {projects.length === 0 ? (
          <button
            onClick={onNewProject}
            className="mb-3 w-full rounded-lg px-2.5 py-[7px] text-left text-sm text-subtle hover:bg-hover hover:text-fg"
          >
            문서를 모아 두고 질문하기
          </button>
        ) : (
          <div className="mb-3">
            {projects.map((p) => (
              <a
                key={p.id}
                href={projectPath(p.id)}
                onClick={(e) => {
                  if (!isPlainClick(e)) return; // ⌘/Ctrl+클릭은 새 탭
                  e.preventDefault();
                  onOpenProject(p.id);
                }}
                className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-[7px] text-left text-sm ${
                  p.id === activeProjectId ? "bg-active text-fg" : "text-fg/85 hover:bg-hover"
                }`}
                title={p.name}
              >
                <FolderIcon className="h-4 w-4 shrink-0 text-muted" />
                <span className="truncate">{p.name}</span>
              </a>
            ))}
          </div>
        )}

        {/* Conversations */}
        <div className="px-2 pb-1 text-xs font-medium text-subtle">최근</div>
        {filtered.length === 0 && (
          <p className="px-2.5 py-2 text-sm text-subtle">
            {query ? "검색 결과가 없습니다" : "아직 대화가 없습니다"}
          </p>
        )}
        {filtered.map((c) => (
          <div
            key={c.id}
            className={`group relative flex items-center rounded-lg text-sm ${
              c.id === activeId ? "bg-active text-fg" : "text-fg/85 hover:bg-hover"
            }`}
          >
            <a
              href={conversationPath(c.id)}
              onClick={(e) => {
                if (!isPlainClick(e)) return; // ⌘/Ctrl+클릭은 새 탭
                e.preventDefault();
                onOpen(c.id);
              }}
              className="flex min-w-0 flex-1 items-center gap-1.5 px-2.5 py-[7px] text-left"
              title={
                c.project_id
                  ? `${projects.find((p) => p.id === c.project_id)?.name ?? "프로젝트"} · ${c.title}`
                  : c.title
              }
            >
              {c.project_id && <FolderIcon className="h-3.5 w-3.5 shrink-0 text-subtle" />}
              <span className="truncate">{c.title}</span>
            </a>
            <button
              onClick={() => onDelete(c.id)}
              className="mr-1 rounded-md p-1 text-subtle opacity-0 hover:bg-hover hover:text-red-500 focus:opacity-100 group-hover:opacity-100"
              aria-label="대화 삭제"
              title="삭제"
            >
              <TrashIcon />
            </button>
          </div>
        ))}
      </nav>

      {/* Footer: 현재 모델, 사용자 */}
      <div className="space-y-2 border-t border-line px-3 py-3">
        <div className="flex items-center gap-2 px-1 text-xs text-muted">
          <span
            className={`h-2 w-2 rounded-full ${info ? "bg-emerald-500" : "bg-subtle"}`}
            aria-hidden
          />
          <span className="truncate">
            {info ? (modelLabel ?? "사용 가능한 모델 없음") : "백엔드 연결 중…"}
          </span>
        </div>
        {user && (
          <div className="flex items-center gap-2.5 rounded-lg px-1 py-1">
            <Avatar user={user} size="h-7 w-7" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-fg">{user.name}</div>
              <div className="flex gap-2 text-[11px] text-muted">
                {onOpenMemory && (
                  <a
                    href={routePath({ kind: "memory" })}
                    onClick={(e) => {
                      if (!isPlainClick(e)) return;
                      e.preventDefault();
                      onOpenMemory();
                    }}
                    className={memoryActive ? "text-fg" : "hover:text-fg"}
                    aria-current={memoryActive ? "page" : undefined}
                  >
                    기억
                  </a>
                )}
                {user.is_admin && (
                  <a href="/admin" className="hover:text-fg">
                    관리
                  </a>
                )}
                {authRequired && onLogout && (
                  <button onClick={onLogout} className="hover:text-fg">
                    로그아웃
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
});
