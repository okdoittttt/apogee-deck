import type { Metadata } from "next";
import { Noto_Serif_KR } from "next/font/google";
import "./globals.css";

// 인사말(display) 전용 serif. 한글 serif 가 없는 환경에서도 같은 모양으로 보이도록 웹 폰트를 쓴다.
// CJK 는 unicode-range 로 쪼개 필요한 글자만 내려받으므로 preload 하지 않는다.
const displaySerif = Noto_Serif_KR({
  weight: ["400", "500"],
  subsets: ["latin"],
  preload: false,
  display: "swap",
  variable: "--font-serif-kr",
});

export const metadata: Metadata = {
  title: "okdoitttt",
  description: "LLM 기반 에이전트",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko" className={displaySerif.variable}>
      <body className="bg-app text-fg antialiased">{children}</body>
    </html>
  );
}
