import { Spin } from "antd";
import { useEffect, useRef } from "react";
import videojs from "video.js";
import "video.js/dist/video-js.css";
import zhCN from "video.js/dist/lang/zh-CN.json";

videojs.addLanguage("zh-CN", zhCN);

export type PlayerControls = {
  seek: (seconds: number, options?: { play?: boolean }) => void;
};

export function VideoPane({
  mediaUrl,
  mimeType,
  emptyText,
  loading = false,
  onReady,
  onTime,
}: {
  mediaUrl: string | null;
  mimeType: string;
  emptyText: string;
  loading?: boolean;
  onReady: (player: PlayerControls) => void;
  onTime?: (seconds: number, playing: boolean) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const onReadyRef = useRef(onReady);
  const onTimeRef = useRef(onTime);
  onReadyRef.current = onReady;
  onTimeRef.current = onTime;

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
    const notify = () => onTimeRef.current?.(player.currentTime() || 0, !element.paused);
    player.on("timeupdate", notify);
    player.on("seeked", notify);
    onReadyRef.current({
      seek: (seconds, options) => {
        player.currentTime(Math.max(0, seconds));
        if (options?.play === false) {
          element.pause();
          return;
        }
        const pending = player.play();
        pending?.catch(() => undefined);
      },
    });
    return () => {
      onReadyRef.current({ seek: () => undefined });
      player.dispose();
      host.replaceChildren();
    };
  }, [mediaUrl, mimeType]);

  if (!mediaUrl) {
    return (
      <div className="video-empty">
        {loading ? (
          <Spin size="large" />
        ) : (
          <div className="empty-state">
            <span className="empty-mark">
              <svg width={22} height={22} viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" strokeWidth="1.6" />
                <path d="M10 9.5l5 2.5-5 2.5v-5z" fill="currentColor" />
              </svg>
            </span>
            <span className="empty-title">{emptyText}</span>
          </div>
        )}
      </div>
    );
  }
  return <div className="video-shell" ref={hostRef} />;
}
