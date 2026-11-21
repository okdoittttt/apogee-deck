/**
 * 화면 주소. 화면은 하나(ChatApp)로 두고 주소만 History API 로 바꾼다 (페이지를 다시 불러오지 않는다).
 * 새로고침하면 next.config.ts 의 rewrite 가 같은 페이지를 돌려주고, 주소를 읽어 그 화면을 연다.
 *
 * - `/`            새 대화
 * - `/c/{id}`      대화 (프로젝트 안의 대화도 같다)
 * - `/p/{id}`      프로젝트 화면
 * - `/p/new`       새 프로젝트 만들기
 */

export type Route =
  | { kind: "new" }
  | { kind: "conversation"; id: string }
  | { kind: "project"; id: string }
  | { kind: "newProject" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** 주소를 화면으로 바꾼다. 모르는 주소나 잘못된 ID 는 새 대화. */
export function parseRoute(pathname: string): Route {
  const [kind, id, ...rest] = pathname.split("/").filter(Boolean);
  if (rest.length > 0) return { kind: "new" };
  if (kind === "c" && id && UUID.test(id)) return { kind: "conversation", id };
  if (kind === "p" && id === "new") return { kind: "newProject" };
  if (kind === "p" && id && UUID.test(id)) return { kind: "project", id };
  return { kind: "new" };
}

/** 화면의 주소. */
export function routePath(route: Route): string {
  switch (route.kind) {
    case "conversation":
      return `/c/${route.id}`;
    case "project":
      return `/p/${route.id}`;
    case "newProject":
      return "/p/new";
    default:
      return "/";
  }
}

export const conversationPath = (id: string) => routePath({ kind: "conversation", id });
export const projectPath = (id: string) => routePath({ kind: "project", id });

/**
 * 링크를 그냥 누른 것인지 (왼쪽 버튼, 보조 키 없음). 아니면(⌘/Ctrl+클릭, 가운데 클릭 등) 브라우저에 맡겨
 * 새 탭에서 열게 한다.
 */
export function isPlainClick(e: { button: number; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean }) {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}
