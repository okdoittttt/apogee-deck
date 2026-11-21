/**
 * 답변 속 출처 번호(`[3]`, `[1, 4]`)를 `#cite-3` 링크로 바꾸는 remark 플러그인.
 * 화면은 이 링크를 출처 번호 칩으로 그린다 (MessageList). 코드·수식·기존 링크 안은 건드리지 않는다.
 */

/** 출처 번호 링크 주소의 앞부분. */
export const CITE_PREFIX = "#cite-";

// mdast 에서 필요한 만큼만 (타입 패키지를 따로 들이지 않는다).
interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

const CITE = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g;
// 이 노드 안의 글자는 출처 번호로 바꾸지 않는다.
const SKIP = new Set(["link", "linkReference", "definition"]);

/** 출처 번호로 볼 수 있는 자리인지: a[1] 같은 첨자와 [0] 은 아니다. 한글 바로 뒤(“입니다[2]”)는 번호로 본다. */
function isCitation(value: string, start: number, numbers: number[]): boolean {
  return !(start > 0 && /[A-Za-z0-9_]/.test(value[start - 1])) && numbers.every((n) => n >= 1);
}

function parseNumbers(group: string): number[] {
  return group.split(",").map((s) => Number(s.trim()));
}

function splitText(value: string): MdNode[] | null {
  const parts: MdNode[] = [];
  let last = 0;
  for (const match of value.matchAll(CITE)) {
    const start = match.index ?? 0;
    const numbers = parseNumbers(match[1]);
    if (!isCitation(value, start, numbers)) continue;
    if (start > last) {
      // 번호 앞 공백은 줄바꿈하지 않는 공백으로: 칩만 다음 줄로 떨어지지 않게 앞 낱말에 붙인다.
      parts.push({ type: "text", value: value.slice(last, start).replace(/\s+$/, "\u00a0") });
    }
    for (const n of numbers) {
      parts.push({ type: "link", url: `${CITE_PREFIX}${n}`, children: [{ type: "text", value: String(n) }] });
    }
    last = start + match[0].length;
  }
  if (parts.length === 0) return null;
  if (last < value.length) parts.push({ type: "text", value: value.slice(last) });
  return parts;
}

function walk(node: MdNode) {
  if (!node.children || SKIP.has(node.type)) return;
  const children: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && child.value) {
      const parts = splitText(child.value);
      if (parts) {
        children.push(...parts);
        continue;
      }
    } else {
      walk(child);
    }
    children.push(child);
  }
  node.children = children;
}

/** react-markdown 의 remarkPlugins 에 넣는다. */
export function remarkCitations() {
  return (tree: MdNode) => walk(tree);
}

/**
 * 답변 글자에서 인용한 출처 번호를 모은다 (칩을 만드는 규칙과 같다). 코드 블록·인라인 코드 안의 번호는 뺀다.
 * 출처 카드를 "답변에 쓴 출처 / 쓰지 않은 출처" 로 나눌 때 쓴다.
 */
export function citedNumbers(text: string): Set<number> {
  const cited = new Set<number>();
  const prose = text.replace(/```[\s\S]*?(```|$)/g, " ").replace(/`[^`\n]*`/g, " ");
  for (const match of prose.matchAll(CITE)) {
    const numbers = parseNumbers(match[1]);
    if (isCitation(prose, match.index ?? 0, numbers)) numbers.forEach((n) => cited.add(n));
  }
  return cited;
}

/** `#cite-3` 링크면 출처 번호(3), 아니면 null. */
export function citationNumber(href: string | undefined): number | null {
  if (!href?.startsWith(CITE_PREFIX)) return null;
  const n = Number(href.slice(CITE_PREFIX.length));
  return Number.isInteger(n) && n > 0 ? n : null;
}
