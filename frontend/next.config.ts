import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Docker 이미지를 작게 만들기 위해 standalone 출력 사용
  output: "standalone",
  // 대화·프로젝트 주소(lib/routes.ts)는 모두 같은 화면이다. 새로고침해도 그 화면을 돌려주고, 주소는 화면이 읽는다.
  async rewrites() {
    return [
      { source: "/c/:id", destination: "/" },
      { source: "/p/:id", destination: "/" },
    ];
  },
};

export default nextConfig;
