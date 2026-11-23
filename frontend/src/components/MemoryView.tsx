"use client";

import { useEffect, useState } from "react";
import {
  type Memory,
  type MemoryCategory,
  type MemoryOverview,
  addMemory,
  deleteAllMemory,
  deleteMemory,
  getMemory,
  saveProfile,
  setMemoryEnabled,
  updateMemory,
} from "@/lib/api";
import { TrashIcon } from "./icons";

/** 카테고리 표시 이름과 순서 (백엔드 app/memory/service.py 의 Category). */
const CATEGORIES: { id: MemoryCategory; label: string }[] = [
  { id: "preferences", label: "선호" },
  { id: "work", label: "일·프로젝트" },
  { id: "personal", label: "생활" },
  { id: "other", label: "기타" },
];

const INPUT =
  "w-full rounded-xl border border-line bg-surface px-3 py-2 text-sm leading-relaxed text-fg placeholder:text-subtle focus:border-line-strong focus:outline-none";
const SELECT =
  "rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-fg focus:border-line-strong focus:outline-none";
const PRIMARY = "rounded-lg bg-fg px-2.5 py-1 text-xs font-medium text-app disabled:opacity-40";
const SECONDARY = "rounded-lg px-2.5 py-1 text-xs text-muted hover:bg-hover hover:text-fg";

const formatDate = (iso: string) =>
  new Date(iso).toLocaleDateString("ko-KR", { year: "numeric", month: "short", day: "numeric" });

/** 켜기/끄기 스위치. */
function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-10 shrink-0 rounded-full transition-colors ${checked ? "bg-fg" : "bg-line-strong"}`}
    >
      <span
        className={`absolute top-0.5 h-5 w-5 rounded-full bg-app shadow transition-[left] ${checked ? "left-[18px]" : "left-0.5"}`}
      />
    </button>
  );
}

/** 기억 하나: 내용과 날짜, 고치기·지우기. */
function MemoryItem({
  memory,
  maxChars,
  onSave,
  onDelete,
}: {
  memory: Memory;
  maxChars: number;
  onSave: (body: { content: string; category: MemoryCategory }) => Promise<boolean>;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(memory.content);
  const [category, setCategory] = useState(memory.category);
  const [saving, setSaving] = useState(false);

  function startEdit() {
    setContent(memory.content);
    setCategory(memory.category);
    setEditing(true);
  }

  async function save() {
    if (!content.trim() || saving) return;
    setSaving(true);
    if (await onSave({ content: content.trim(), category })) setEditing(false);
    setSaving(false);
  }

  if (editing) {
    return (
      <li className="space-y-2 px-3.5 py-3">
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          maxLength={maxChars}
          rows={2}
          autoFocus
          className={INPUT}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void save();
            } else if (e.key === "Escape") setEditing(false);
          }}
        />
        <div className="flex items-center justify-between gap-2">
          <select value={category} onChange={(e) => setCategory(e.target.value as MemoryCategory)} className={SELECT}>
            {CATEGORIES.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
          <div className="flex gap-2">
            <button onClick={() => setEditing(false)} className={SECONDARY}>
              취소
            </button>
            <button onClick={() => void save()} disabled={!content.trim() || saving} className={PRIMARY}>
              저장
            </button>
          </div>
        </div>
      </li>
    );
  }

  return (
    <li className="group flex items-start gap-3 px-3.5 py-2.5">
      <button onClick={startEdit} className="min-w-0 flex-1 text-left" title="고치기">
        <span className="block whitespace-pre-wrap break-words text-sm text-fg">{memory.content}</span>
        <span className="text-[11px] text-subtle">{formatDate(memory.updated_at)}</span>
      </button>
      <button
        onClick={onDelete}
        className="rounded-md p-1 text-subtle opacity-0 hover:bg-hover hover:text-red-500 focus:opacity-100 group-hover:opacity-100"
        aria-label="기억 삭제"
        title="삭제"
      >
        <TrashIcon />
      </button>
    </li>
  );
}

/**
 * 기억 관리 화면 (/memory): 기억 켜기/끄기, 프로필, 카테고리별 기억 목록.
 * 대화 중에 AI 가 저장한 기억도 여기서 보고 고친다. 열 때마다 새로 읽는다.
 */
export function MemoryView({ onToast }: { onToast: (message: string) => void }) {
  const [data, setData] = useState<MemoryOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [profile, setProfile] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [newCategory, setNewCategory] = useState<MemoryCategory>("preferences");
  const [adding, setAdding] = useState(false);
  const [confirmingClear, setConfirmingClear] = useState(false);

  useEffect(() => {
    getMemory()
      .then((d) => {
        setData(d);
        setProfile(d.profile.text);
      })
      .catch((e) => setLoadError((e as Error).message));
  }, []);

  if (loadError && !data) {
    return <p className="mx-auto mt-16 max-w-2xl px-4 text-sm text-muted">{loadError}</p>;
  }
  if (!data) return null;

  const update = (patch: Partial<MemoryOverview>) => setData((d) => (d ? { ...d, ...patch } : d));
  const profileDirty = profile !== data.profile.text;
  const full = data.memories.length >= data.max_memories;

  async function toggle(enabled: boolean) {
    update({ enabled });
    try {
      await setMemoryEnabled(enabled);
    } catch (e) {
      update({ enabled: !enabled });
      onToast((e as Error).message);
    }
  }

  async function submitProfile() {
    setSavingProfile(true);
    try {
      const saved = await saveProfile(profile.trim());
      update({ profile: saved });
      setProfile(saved.text);
    } catch (e) {
      onToast((e as Error).message);
    } finally {
      setSavingProfile(false);
    }
  }

  async function submitMemory() {
    if (!newContent.trim() || adding || !data) return;
    setAdding(true);
    try {
      const memory = await addMemory(newContent.trim(), newCategory);
      update({ memories: [memory, ...data.memories] });
      setNewContent("");
    } catch (e) {
      onToast((e as Error).message);
    } finally {
      setAdding(false);
    }
  }

  async function saveMemory(id: string, body: { content: string; category: MemoryCategory }) {
    try {
      const saved = await updateMemory(id, body);
      setData((d) => d && { ...d, memories: [saved, ...d.memories.filter((m) => m.id !== id)] });
      return true;
    } catch (e) {
      onToast((e as Error).message);
      return false;
    }
  }

  async function removeMemory(id: string) {
    try {
      await deleteMemory(id);
      setData((d) => d && { ...d, memories: d.memories.filter((m) => m.id !== id) });
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  async function clearAll() {
    try {
      await deleteAllMemory();
      update({ memories: [], profile: { text: "", updated_at: null } });
      setProfile("");
      setConfirmingClear(false);
    } catch (e) {
      onToast((e as Error).message);
    }
  }

  const groups = CATEGORIES.map((c) => ({ ...c, items: data.memories.filter((m) => m.category === c.id) })).filter(
    (g) => g.items.length > 0,
  );

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-4 pb-16 pt-10">
        <h2 className="mb-2 font-display text-[28px] text-fg">기억</h2>
        <p className="mb-8 text-sm text-muted">
          AI 가 대화에서 알게 된 정보를 기억해 두고 다른 대화에서도 참고합니다. 대화 중에 &ldquo;기억해 줘&rdquo;,
          &ldquo;잊어 줘&rdquo;라고 말하거나 여기서 직접 고칠 수 있습니다.
        </p>

        {!data.available ? (
          <p className="rounded-xl border border-line px-4 py-3 text-sm text-muted">
            서버에서 기억 기능이 꺼져 있습니다.
          </p>
        ) : (
          <>
            <section className="mb-10 flex items-start justify-between gap-4 rounded-xl border border-line px-4 py-3">
              <div>
                <h3 className="text-sm font-medium text-fg">기억 사용</h3>
                <p className="mt-0.5 text-xs text-muted">
                  끄면 저장된 기억은 남아 있지만, 대화에서 참고하거나 새로 저장하지 않습니다.
                </p>
              </div>
              <Switch checked={data.enabled} onChange={(v) => void toggle(v)} label="기억 사용" />
            </section>

            <section className="mb-10">
              <h3 className="text-sm font-medium text-fg">프로필</h3>
              <p className="mb-2 mt-0.5 text-xs text-muted">
                모든 대화에서 항상 참고하는 핵심 정보입니다. 이름, 하는 일, 원하는 답변 방식처럼 잘 바뀌지 않는 것을
                짧게 적어 주세요.
              </p>
              <textarea
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
                maxLength={data.max_profile_chars}
                rows={4}
                placeholder={"예)\n이름: 홍길동\n하는 일: 백엔드 개발자 (Python, FastAPI)\n답변은 짧고 핵심 위주로"}
                className={`${INPUT} resize-y`}
              />
              <div className="mt-1.5 flex items-center justify-between">
                <span className="text-[11px] text-subtle">
                  {profile.length.toLocaleString()} / {data.max_profile_chars.toLocaleString()}자
                  {data.profile.updated_at && ` · ${formatDate(data.profile.updated_at)} 수정`}
                </span>
                {profileDirty && (
                  <div className="flex gap-2">
                    <button onClick={() => setProfile(data.profile.text)} className={SECONDARY}>
                      취소
                    </button>
                    <button onClick={() => void submitProfile()} disabled={savingProfile} className={PRIMARY}>
                      저장
                    </button>
                  </div>
                )}
              </div>
            </section>

            <section className="mb-10">
              <div className="mb-2 flex items-baseline justify-between">
                <h3 className="text-sm font-medium text-fg">기억</h3>
                <span className="text-[11px] text-subtle">
                  {data.memories.length} / {data.max_memories}개
                </span>
              </div>

              <div className="mb-4 flex gap-2">
                <select
                  value={newCategory}
                  onChange={(e) => setNewCategory(e.target.value as MemoryCategory)}
                  className={SELECT}
                  aria-label="카테고리"
                >
                  {CATEGORIES.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </select>
                <input
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.nativeEvent.isComposing) void submitMemory();
                  }}
                  maxLength={data.max_memory_chars}
                  disabled={full}
                  placeholder={full ? "기억이 가득 찼습니다. 필요 없는 기억을 지워 주세요." : "기억할 내용 (예: 채식주의자다)"}
                  className={`${INPUT} min-w-0 flex-1`}
                />
                <button
                  onClick={() => void submitMemory()}
                  disabled={!newContent.trim() || adding || full}
                  className={`${PRIMARY} shrink-0 px-3`}
                >
                  추가
                </button>
              </div>

              {groups.length === 0 ? (
                <p className="text-sm text-subtle">아직 기억한 내용이 없습니다.</p>
              ) : (
                <div className="space-y-5">
                  {groups.map((g) => (
                    <div key={g.id}>
                      <h4 className="mb-1.5 text-xs font-medium text-muted">
                        {g.label} <span className="font-normal text-subtle">{g.items.length}</span>
                      </h4>
                      <ul className="divide-y divide-line rounded-xl border border-line">
                        {g.items.map((m) => (
                          <MemoryItem
                            key={m.id}
                            memory={m}
                            maxChars={data.max_memory_chars}
                            onSave={(body) => saveMemory(m.id, body)}
                            onDelete={() => void removeMemory(m.id)}
                          />
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {confirmingClear ? (
              <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-300 px-4 py-3 dark:border-red-900">
                <span className="flex-1 text-sm text-fg">
                  프로필과 기억 {data.memories.length}개를 모두 지울까요? 되돌릴 수 없습니다.
                </span>
                <div className="flex gap-2">
                  <button onClick={() => setConfirmingClear(false)} className={SECONDARY}>
                    취소
                  </button>
                  <button
                    onClick={() => void clearAll()}
                    className="rounded-lg bg-red-600 px-2.5 py-1 text-xs font-medium text-white"
                  >
                    모두 지우기
                  </button>
                </div>
              </div>
            ) : (
              <button onClick={() => setConfirmingClear(true)} className="text-xs text-subtle hover:text-red-500">
                모든 기억 지우기
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
