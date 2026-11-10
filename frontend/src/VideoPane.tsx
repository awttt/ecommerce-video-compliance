import { useEffect, useRef } from "react";
import videojs from "video.js";
import "video.js/dist/video-js.css";
import zhCN from "video.js/dist/lang/zh-CN.json";

videojs.addLanguage("zh-CN", zhCN);

export function VideoPane({
  mediaUrl,
  mimeType,
  emptyText,
  onReady,
}: {
  mediaUrl: string | null;
  mimeType: string;
  emptyText: string;
  onReady: (seek: (seconds: number) => void) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !mediaUrl) return undefined;
    const element = document.createElement("video");
    element.className = "video-js vjs-big-play-centered";
    element.setAttribute("playsinline", "true");
    host.replaceChildren(element);
    const player = videojs(element, {
      controls: true,
      preload: "auto",
      language: "zh-CN",
      sources: [{ src: mediaUrl, type: mimeType || "video/mp4" }],
    });
    onReadyRef.current((seconds: number) => {
      player.currentTime(Math.max(0, seconds));
      const pending = player.play();
      pending?.catch(() => undefined);
    });
    return () => {
      onReadyRef.current(() => undefined);
      player.dispose();
      host.replaceChildren();
    };
  }, [mediaUrl, mimeType]);

  if (!mediaUrl) {
    return <div className="video-empty">{emptyText}</div>;
  }
  return <div className="video-shell" ref={hostRef} />;
}
