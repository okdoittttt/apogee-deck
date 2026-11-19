export const MAX_IMAGES = 4;
// 긴 변 기준 최대 픽셀. 더 크게 보내도 모델이 내부에서 줄이므로 토큰/전송량만 늘어난다.
const MAX_DIMENSION = 1024;
const JPEG_QUALITY = 0.85;

export interface Attachment {
  id: string;
  mimeType: string;
  base64: string; // data URL prefix 없음
  previewUrl: string; // data URL (미리보기 / 낙관적 렌더링용)
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("이미지를 읽을 수 없습니다."));
    img.src = src;
  });
}

/** 이미지를 MAX_DIMENSION 이하로 줄이고 JPEG(투명도가 있으면 PNG)로 인코딩한다. */
export async function fileToAttachment(file: File): Promise<Attachment> {
  if (!file.type.startsWith("image/")) throw new Error(`이미지 파일이 아닙니다: ${file.name}`);

  const objectUrl = URL.createObjectURL(file);
  try {
    const img = await loadImage(objectUrl);
    const scale = Math.min(1, MAX_DIMENSION / Math.max(img.width, img.height));
    const width = Math.round(img.width * scale);
    const height = Math.round(img.height * scale);

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas 를 사용할 수 없습니다.");
    ctx.drawImage(img, 0, 0, width, height);

    const mimeType = file.type === "image/png" ? "image/png" : "image/jpeg";
    const dataUrl = canvas.toDataURL(mimeType, JPEG_QUALITY);
    return {
      id: crypto.randomUUID(),
      mimeType,
      base64: dataUrl.slice(dataUrl.indexOf(",") + 1),
      previewUrl: dataUrl,
    };
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}
