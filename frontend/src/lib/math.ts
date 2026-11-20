/**
 * 모델 답변의 LaTeX 구분자를 remark-math 가 알아듣는 형태로 바꾼다.
 *
 * 모델은 수식을 `\( … \)`(문장 안)와 `\[ … \]`(별도 줄)로 쓰는데, remark-math 는 `$` 구분자만 안다.
 * 그대로 두면 Markdown 이 `\[` 를 이스케이프로 읽어 역슬래시를 지우고 `_` 를 기울임으로 바꾼다.
 *
 * - `\( x \)` → `$$x$$` (문장 안 수식)
 * - 줄 첫머리의 `\[ x \]` → 앞뒤 줄을 띄운 `$$` 블록 (별도 줄 수식). 들여쓰기는 그 줄을 따라서 목록 안에서도 유지된다.
 * - 줄 중간의 `\[ x \]` → `$$x$$` (블록으로 바꾸면 문단이나 목록이 끊긴다)
 *
 * `$` 한 개 수식은 렌더러에서 꺼 둔다 (`singleDollarTextMath: false`). "$5" 같은 금액이 수식이 되지 않게 하려는 것이다.
 * 코드 블록과 인라인 코드 안은 바꾸지 않는다. 닫히지 않은 구분자(스트리밍 중)는 닫힐 때까지 그대로 둔다.
 * 기호·숫자가 없는 내용(`\[주의\]` 처럼 괄호를 이스케이프한 글)도 수식으로 보지 않는다.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** 수식으로 볼 내용. `\[주의\]` 처럼 괄호를 이스케이프한 일반 글은 그대로 둔다. */
const LOOKS_LIKE_MATH = /[\\^_=+\-*/<>{}0-9]|^[A-Za-z]{1,3}$/;

export function normalizeMath(markdown: string): string {
  if (!markdown.includes("\\(") && !markdown.includes("\\[")) return markdown;
  const lines = markdown.split("\n");
  const out: string[] = [];
  let prose: string[] = [];
  let fence: string | null = null;

  const flush = () => {
    if (prose.length) out.push(convertProse(prose.join("\n")));
    prose = [];
  };

  for (const line of lines) {
    const match = FENCE.exec(line);
    if (fence) {
      out.push(line);
      if (match && match[1][0] === fence[0] && match[1].length >= fence.length) fence = null;
    } else if (match) {
      flush();
      fence = match[1];
      out.push(line);
    } else {
      prose.push(line);
    }
  }
  flush();
  return out.join("\n");
}

/** 코드 블록 밖의 글을 바꾼다. 인라인 코드(`…`)는 건너뛴다. */
function convertProse(text: string): string {
  let result = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "`") {
      const run = /^`+/.exec(text.slice(i))![0];
      const close = text.indexOf(run, i + run.length);
      const end = close < 0 ? i + run.length : close + run.length;
      result += text.slice(i, end);
      i = end;
      continue;
    }
    if (ch === "\\" && (text[i + 1] === "(" || text[i + 1] === "[")) {
      const display = text[i + 1] === "[";
      const close = text.indexOf(display ? "\\]" : "\\)", i + 2);
      if (close >= 0) {
        const body = text.slice(i + 2, close).trim();
        if (LOOKS_LIKE_MATH.test(body)) {
          result += display ? displayMath(result, body) : `$$${body}$$`;
          i = close + 2;
          continue;
        }
      }
    }
    result += ch;
    i += 1;
  }
  return result;
}

/** 줄 첫머리면 `$$` 블록으로, 줄 중간이면 문장 안 수식으로 바꾼다. */
function displayMath(before: string, body: string): string {
  const lineStart = before.lastIndexOf("\n") + 1;
  const prefix = before.slice(lineStart);
  if (prefix.trim()) return `$$${body}$$`;
  const indent = prefix;
  const inner = body
    .split("\n")
    .map((l) => indent + l.trim())
    .join("\n");
  // 앞줄이 문단이면 블록이 문단에 붙지 않도록 빈 줄을 둔다
  return `\n${indent}$$\n${inner}\n${indent}$$\n`;
}
