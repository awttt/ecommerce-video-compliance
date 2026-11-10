/// <reference types="vite/client" />

declare module "video.js/dist/lang/zh-CN.json" {
  const phrases: Record<string, string>;
  export default phrases;
}

declare module "video.js" {
  interface Player {
    currentTime(seconds?: number): number;
    play(): Promise<void> | undefined;
    dispose(): void;
    on(event: string, handler: () => void): void;
  }
  interface VideoJsOptions {
    controls?: boolean;
    preload?: string;
    language?: string;
    sources?: { src: string; type?: string }[];
  }
  function videojs(element: HTMLVideoElement, options?: VideoJsOptions): Player;
  namespace videojs {
    function addLanguage(code: string, phrases: Record<string, string>): void;
  }
  export default videojs;
}
