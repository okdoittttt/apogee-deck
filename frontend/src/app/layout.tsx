import type { Metadata } from "next";
// 앱 전체 글꼴(Pretendard 가변 폰트). 동적 서브셋은 unicode-range 로 쪼개져 있어 화면에 쓰인 글자 조각만 내려받는다.
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Apogee Deck",
  description: "LLM 기반 에이전트",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body className="bg-app text-fg antialiased">{children}</body>
    </html>
  );
}
