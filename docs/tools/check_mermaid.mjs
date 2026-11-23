// Markdown 안의 ```mermaid 블록을 실제 mermaid 파서로 검사한다.
//
// GitHub 는 문법이 틀린 그림을 오류 상자로 보여 준다. 예약어 노드 ID(end, graph, call …)처럼
// 눈으로 찾기 어려운 실수를 커밋 전에 잡으려고 쓴다. 브라우저 없이 jsdom 위에서 parse 만 한다.
//
// 사용법: node check_mermaid.mjs <md 파일 ...>   (make docs-check)
import fs from "node:fs";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Element = dom.window.Element;

const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false });

let total = 0;
let bad = 0;
for (const file of process.argv.slice(2)) {
  const src = fs.readFileSync(file, "utf8");
  for (const match of src.matchAll(/```mermaid\n([\s\S]*?)```/g)) {
    total++;
    const line = src.slice(0, match.index).split("\n").length;
    try {
      await mermaid.parse(match[1]);
    } catch (e) {
      bad++;
      console.log(`${file}:${line}: ${String(e.message || e).slice(0, 400)}`);
    }
  }
}
console.log(`mermaid: ${total - bad}/${total} ok`);
process.exit(bad ? 1 : 0);
