import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Docker 이미지를 작게 만들기 위해 standalone 출력 사용
  output: "standalone",
};

export default nextConfig;
