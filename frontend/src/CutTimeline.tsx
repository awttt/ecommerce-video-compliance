import { Slider } from "antd";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { CATEGORY_COLOR, type CutSpan, type Finding } from "./types";

const MIN_CUT = 0.05;
const SNAP_PX = 6;
const PAINT_PX = 8;
const NUDGE_SEC = 0.1;

type Edge = "start" | "end";
type Drag =
  | { kind: "playhead"; originX: number }
  | { kind: "resize"; index: number; edge: Edge; originX: number }
  | { kind: "move"; index: number; originStart: number; originEnd: number; originSec: number; originX: number }
  | { kind: "paint"; originSec: number; currentSec: number; originX: number };

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function formatSec(value: number): string {
  const sec = Math.max(0, value);
  const minutes = Math.floor(sec / 60);
  const rest = sec - minutes * 60;
  return `${minutes}:${rest.toFixed(2).padStart(5, "0")}`;
}

function tickStep(span: number): number {
  const rough = span / 8;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  return steps.find((step) => step >= rough) ?? 600;
}

function formatTick(sec: number): string {
  const minutes = Math.floor(sec / 60);
  const seconds = Math.round((sec - minutes * 60) * 10) / 10;
  const text = Number.isInteger(seconds) ? String(seconds).padStart(2, "0") : seconds.toFixed(1);
  return `${minutes}:${text}`;
}

export function keepGaps(cuts: CutSpan[], duration: number): CutSpan[] {
  const gaps: CutSpan[] = [];
  let cursor = 0;
  for (const cut of mergeCuts(cuts)) {
    if (cut.start_sec - cursor >= MIN_CUT) gaps.push({ start_sec: cursor, end_sec: cut.start_sec });
    cursor = Math.max(cursor, cut.end_sec);
  }
  if (duration - cursor >= MIN_CUT) gaps.push({ start_sec: cursor, end_sec: duration });
  return gaps;
}

export function insertCut(cuts: CutSpan[], duration: number, playhead: number, length = 1): CutSpan[] | null {
  const gaps = keepGaps(cuts, duration);
  if (gaps.length === 0 || duration <= 0) return null;
  const inside = gaps.find((gap) => playhead >= gap.start_sec && playhead < gap.end_sec - 0.001);
  const gap = inside ?? gaps.find((item) => item.start_sec >= playhead) ?? gaps[gaps.length - 1];
  const room = gap.end_sec - gap.start_sec;
  const span = Math.min(length, room);
  const start = inside ? clamp(playhead, gap.start_sec, gap.end_sec - span) : gap.start_sec;
  return mergeCuts([...cuts, { start_sec: start, end_sec: start + span }]);
}

export function mergeCuts(cuts: CutSpan[]): CutSpan[] {
  const ordered = cuts
    .map((cut) => ({
      start_sec: Math.min(cut.start_sec, cut.end_sec),
      end_sec: Math.max(cut.start_sec, cut.end_sec),
    }))
    .filter((cut) => cut.end_sec - cut.start_sec >= MIN_CUT)
    .sort((left, right) => left.start_sec - right.start_sec);
  const merged: CutSpan[] = [];
  for (const cut of ordered) {
    const last = merged[merged.length - 1];
    if (last && cut.start_sec <= last.end_sec + 0.001) {
      last.end_sec = Math.max(last.end_sec, cut.end_sec);
    } else {
      merged.push({ ...cut });
    }
  }
  return merged;
}

export function snapSecond(sec: number, duration: number, width: number, targets: number[]): number {
  const bounded = clamp(sec, 0, duration);
  if (width <= 0 || duration <= 0) return bounded;
  const limit = (SNAP_PX / width) * duration;
  let best = bounded;
  let dist = limit;
  for (const target of targets) {
    const gap = Math.abs(target - bounded);
    if (gap <= dist) {
      best = target;
      dist = gap;
    }
  }
  return clamp(best, 0, duration);
}

function snapTargets(cuts: CutSpan[], findings: Finding[], playhead: number, ignoreIndex: number): number[] {
  const targets = [playhead];
  findings.forEach((finding) => {
    targets.push(finding.start_sec, finding.end_sec);
  });
  cuts.forEach((cut, index) => {
    if (index === ignoreIndex) return;
    targets.push(cut.start_sec, cut.end_sec);
  });
  return targets;
}

function sentenceCaption(cut: CutSpan, findings: Finding[]): string {
  const lines: string[] = [];
  const seen = new Set<string>();
  for (const finding of findings) {
    if (finding.来源 !== "口播" || finding.sentence_start == null || finding.sentence_end == null) continue;
    if (finding.end_sec <= cut.start_sec || finding.start_sec >= cut.end_sec) continue;
    const text = `${formatSec(finding.sentence_start)} – ${formatSec(finding.sentence_end)}`;
    if (seen.has(text)) continue;
    seen.add(text);
    lines.push(text);
  }
  if (lines.length === 0) return "";
  const shown = lines.slice(0, 3).join("；");
  return lines.length > 3 ? `整句 ${shown} 等` : `整句 ${shown}`;
}

function typingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(target.closest("input, textarea, select, .ant-slider"));
}

export function CutTimeline({
  jobId,
  duration,
  findings,
  cuts,
  currentTime,
  activeKey,
  onSeek,
  onChange,
  onGesture,
  onSelectFinding,
  onSelect,
  onActivate,
  findingKey,
}: {
  jobId: string;
  duration: number;
  findings: Finding[];
  cuts: CutSpan[];
  currentTime: number;
  activeKey: string;
  onSeek: (seconds: number) => void;
  onChange: (cuts: CutSpan[]) => void;
  onGesture: (active: boolean) => void;
  onSelectFinding: (finding: Finding) => void;
  onSelect?: (cut: CutSpan | null) => void;
  onActivate?: (cut: CutSpan) => void;
  findingKey: (finding: Finding) => string;
}) {
  const editorRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<Drag | null>(null);
  const cutsRef = useRef(cuts);
  const maxZoomRef = useRef(1);
  const onSelectRef = useRef(onSelect);
  cutsRef.current = cuts;
  onSelectRef.current = onSelect;
  const seekFrame = useRef(0);
  const [draft, setDraft] = useState<CutSpan | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [activeEdge, setActiveEdge] = useState<Edge>("end");
  const [zoom, setZoom] = useState(1);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [hiddenFrames, setHiddenFrames] = useState<Set<number>>(new Set());
  const selectedCut = selected != null ? cuts[selected] : undefined;
  const frameCount = Math.max(1, Math.ceil(duration));
  const fit = duration > 0 && viewportWidth > 0 ? viewportWidth / duration : 0;
  const maxZoom = fit > 0 ? Math.max(1, 80 / fit) : 1;
  maxZoomRef.current = maxZoom;
  const zoomValue = Math.min(zoom, maxZoom);
  const pxPerSec = fit > 0 ? fit * zoomValue : 0;
  const sheetWidth = pxPerSec > 0 ? Math.max(viewportWidth, duration * pxPerSec) : undefined;

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return undefined;
    const update = () => setViewportWidth(element.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return undefined;
    const onWheel = (event: WheelEvent) => {
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      event.preventDefault();
      const factor = event.deltaY > 0 ? 0.9 : 1.1;
      setZoom((value) => clamp(value * factor, 1, maxZoomRef.current));
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    const scroller = viewportRef.current;
    if (!scroller || !sheetWidth || duration <= 0) return;
    const x = (clamp(currentTime, 0, duration) / duration) * sheetWidth;
    const left = scroller.scrollLeft;
    const right = left + scroller.clientWidth;
    if (x < left + 32 || x > right - 32) {
      scroller.scrollLeft = Math.max(0, x - scroller.clientWidth * 0.35);
    }
  }, [currentTime, sheetWidth, duration]);

  useEffect(() => {
    onSelectRef.current?.(selected != null ? cuts[selected] ?? null : null);
  }, [selected, cuts]);

  useEffect(() => {
    setHiddenFrames(new Set());
  }, [jobId]);

  function widthOf(element: HTMLElement | null): number {
    return element?.getBoundingClientRect().width ?? 0;
  }

  function secFromClient(clientX: number): number {
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || duration <= 0) return 0;
    return clamp(((clientX - rect.left) / rect.width) * duration, 0, duration);
  }

  function focusEditor() {
    editorRef.current?.focus();
  }

  function scrub(sec: number) {
    if (seekFrame.current) return;
    seekFrame.current = window.requestAnimationFrame(() => {
      seekFrame.current = 0;
      onSeek(sec);
    });
  }

  function begin(event: ReactPointerEvent, drag: Drag) {
    event.stopPropagation();
    event.preventDefault();
    stageRef.current?.setPointerCapture(event.pointerId);
    dragRef.current = drag;
    onGesture(true);
    focusEditor();
  }

  function commit(index: number, start: number, end: number, edge: Edge) {
    const merged = mergeCuts(cutsRef.current.map((cut, item) => (item === index ? { start_sec: start, end_sec: end } : cut)));
    cutsRef.current = merged;
    const mid = (Math.max(0, start) + Math.min(duration, end)) / 2;
    const nextIndex = merged.findIndex((cut) => mid >= cut.start_sec && mid <= cut.end_sec);
    setSelected(nextIndex >= 0 ? nextIndex : null);
    setActiveEdge(edge);
    onChange(merged);
    const chosen = nextIndex >= 0 ? merged[nextIndex] : undefined;
    if (chosen) onSeek(edge === "start" ? chosen.start_sec : chosen.end_sec);
  }

  function placeEdge(edge: Edge, target: number) {
    if (selected == null) return;
    const cut = cutsRef.current[selected];
    if (!cut) return;
    let start = cut.start_sec;
    let end = cut.end_sec;
    if (edge === "start") start = Math.min(Math.max(0, target), end - MIN_CUT);
    else end = Math.max(Math.min(duration, target), start + MIN_CUT);
    commit(selected, start, end, edge);
  }

  function finishResize(index: number, edge: Edge, start: number, end: number) {
    const width = widthOf(stageRef.current);
    const targets = snapTargets(cutsRef.current, findings, currentTime, index);
    if (edge === "start") start = snapSecond(start, duration, width, targets);
    else end = snapSecond(end, duration, width, targets);
    if (end - start < MIN_CUT) {
      if (edge === "start") start = end - MIN_CUT;
      else end = start + MIN_CUT;
    }
    commit(index, clamp(start, 0, duration), clamp(end, 0, duration), edge);
  }

  function finishMove(index: number, start: number, end: number) {
    const length = end - start;
    const width = widthOf(stageRef.current);
    const targets = snapTargets(cutsRef.current, findings, currentTime, index);
    const snappedStart = snapSecond(start, duration, width, targets);
    const snappedEnd = snapSecond(end, duration, width, targets);
    const startGap = Math.abs(snappedStart - start);
    const endGap = Math.abs(snappedEnd - end);
    if (startGap > 0 || endGap > 0) {
      if (endGap > startGap) {
        end = snappedEnd;
        start = end - length;
      } else {
        start = snappedStart;
        end = start + length;
      }
    }
    if (start < 0) {
      end -= start;
      start = 0;
    }
    if (end > duration) {
      start -= end - duration;
      end = duration;
    }
    const merged = mergeCuts(cutsRef.current.map((cut, item) => (item === index ? { start_sec: start, end_sec: end } : cut)));
    cutsRef.current = merged;
    const mid = (Math.max(0, start) + Math.min(duration, end)) / 2;
    const nextIndex = merged.findIndex((cut) => mid >= cut.start_sec && mid <= cut.end_sec);
    setSelected(nextIndex >= 0 ? nextIndex : null);
    onChange(merged);
  }

  function onPointerMove(event: ReactPointerEvent) {
    const drag = dragRef.current;
    if (!drag) return;
    const sec = secFromClient(event.clientX);
    if (drag.kind === "playhead") {
      scrub(sec);
      return;
    }
    if (drag.kind === "paint") {
      drag.currentSec = sec;
      setDraft({ start_sec: Math.min(drag.originSec, sec), end_sec: Math.max(drag.originSec, sec) });
      scrub(sec);
      return;
    }
    if (drag.kind === "resize") {
      const current = cutsRef.current[drag.index];
      if (!current) return;
      let start = current.start_sec;
      let end = current.end_sec;
      if (drag.edge === "start") start = Math.min(sec, end - MIN_CUT);
      else end = Math.max(sec, start + MIN_CUT);
      const next = cutsRef.current.map((cut, index) => (index === drag.index ? { start_sec: start, end_sec: end } : cut));
      cutsRef.current = next;
      onChange(next);
      scrub(drag.edge === "start" ? start : end);
      return;
    }
    const length = drag.originEnd - drag.originStart;
    const start = clamp(drag.originStart + (sec - drag.originSec), 0, Math.max(0, duration - length));
    const next = cutsRef.current.map((cut, index) =>
      index === drag.index ? { start_sec: start, end_sec: start + length } : cut,
    );
    cutsRef.current = next;
    onChange(next);
    scrub(sec);
  }

  function onPointerUp(event: ReactPointerEvent) {
    const drag = dragRef.current;
    dragRef.current = null;
    setDraft(null);
    if (stageRef.current?.hasPointerCapture(event.pointerId)) {
      stageRef.current.releasePointerCapture(event.pointerId);
    }
    onGesture(false);
    if (!drag) return;
    if (drag.kind === "playhead") return;
    if (drag.kind === "paint") {
      if (Math.abs(event.clientX - drag.originX) < PAINT_PX) {
        onSeek(drag.originSec);
        return;
      }
      const start = Math.min(drag.originSec, drag.currentSec);
      const end = Math.max(drag.originSec, drag.currentSec);
      if (end - start < MIN_CUT) return;
      const width = widthOf(stageRef.current);
      const targets = snapTargets(cutsRef.current, findings, currentTime, -1);
      const snapped = {
        start_sec: snapSecond(start, duration, width, targets),
        end_sec: snapSecond(end, duration, width, targets),
      };
      if (snapped.end_sec - snapped.start_sec < MIN_CUT) return;
      const merged = mergeCuts([...cutsRef.current, snapped]);
      cutsRef.current = merged;
      const mid = (snapped.start_sec + snapped.end_sec) / 2;
      const index = merged.findIndex((cut) => mid >= cut.start_sec && mid <= cut.end_sec);
      setSelected(index >= 0 ? index : null);
      setActiveEdge("end");
      onChange(merged);
      if (index >= 0) onActivate?.(merged[index]);
      return;
    }
    const current = cutsRef.current[drag.index];
    if (!current) return;
    if (Math.abs(event.clientX - drag.originX) < 4) return;
    if (drag.kind === "resize") finishResize(drag.index, drag.edge, current.start_sec, current.end_sec);
    else finishMove(drag.index, current.start_sec, current.end_sec);
  }

  function restore(index: number) {
    const next = cuts.filter((_, item) => item !== index);
    cutsRef.current = next;
    setSelected(null);
    onChange(next);
  }

  function addSegment() {
    const before = cutsRef.current;
    const next = insertCut(before, duration, currentTime);
    if (!next) return;
    cutsRef.current = next;
    const previous = new Set(before.map((cut) => `${cut.start_sec}-${cut.end_sec}`));
    const index = next.findIndex((cut) => !previous.has(`${cut.start_sec}-${cut.end_sec}`));
    const chosen = index >= 0 ? index : next.length - 1;
    setSelected(chosen);
    setActiveEdge("end");
    onChange(next);
    onSeek(next[chosen].start_sec);
    onActivate?.(next[chosen]);
    focusEditor();
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (typingTarget(event.target)) return;
    if ((event.key === "Delete" || event.key === "Backspace") && selected != null) {
      event.preventDefault();
      restore(selected);
      return;
    }
    if (selected == null) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const cut = cutsRef.current[selected];
      if (!cut) return;
      const delta = event.key === "ArrowLeft" ? -NUDGE_SEC : NUDGE_SEC;
      const base = activeEdge === "start" ? cut.start_sec : cut.end_sec;
      placeEdge(activeEdge, base + delta);
      return;
    }
    if (event.key === "[") {
      event.preventDefault();
      placeEdge("start", currentTime);
    } else if (event.key === "]") {
      event.preventDefault();
      placeEdge("end", currentTime);
    }
  }

  const ticks: number[] = [];
  const step = tickStep(duration / Math.max(zoomValue, 1));
  for (let sec = 0; sec <= duration + 0.001; sec += step) ticks.push(Math.min(duration, sec));
  const caption = selectedCut ? sentenceCaption(selectedCut, findings) : "";

  return (
    <div className="cut-editor" ref={editorRef} tabIndex={0} onKeyDown={onKeyDown}>
      <div className="cut-scroll" ref={viewportRef}>
        <div className="cut-sheet" style={sheetWidth ? { width: sheetWidth } : undefined}>
          <div
            className="cut-ruler"
            onPointerDown={(event) => {
              const sec = secFromClient(event.clientX);
              begin(event, { kind: "playhead", originX: event.clientX });
              scrub(sec);
            }}
          >
            {ticks.map((sec) => (
              <span key={sec} style={{ left: `${duration > 0 ? (sec / duration) * 100 : 0}%` }}>
                {formatTick(sec)}
              </span>
            ))}
          </div>
          <div
            className="cut-stage"
            ref={stageRef}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
          >
            <div className="cut-hits">
              {findings.map((finding) => (
                <button
                  key={findingKey(finding)}
                  type="button"
                  className={`cut-hit${finding.来源 === "口播" ? " speech" : ""}${activeKey === findingKey(finding) ? " active" : ""}`}
                  style={{
                    left: `${(finding.start_sec / duration) * 100}%`,
                    width: `${Math.max(((finding.end_sec - finding.start_sec) / duration) * 100, 0.4)}%`,
                    background: CATEGORY_COLOR[finding.类别] || "#8b968f",
                  }}
                  title={`${finding.时间点} ${finding.来源} ${finding.类别} ${finding.原文}`}
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelectFinding(finding);
                  }}
                />
              ))}
            </div>
            <div
              className="cut-track"
              onPointerDown={(event) => {
                if (event.target !== event.currentTarget) return;
                const sec = secFromClient(event.clientX);
                begin(event, { kind: "paint", originSec: sec, currentSec: sec, originX: event.clientX });
              }}
            >
              <div className="cut-film">
                {Array.from({ length: frameCount }, (_, index) =>
                  hiddenFrames.has(index) ? null : (
                    <img
                      key={index}
                      alt=""
                      src={`/api/jobs/${jobId}/strip/${index}`}
                      style={{ left: `${(index / frameCount) * 100}%`, width: `${100 / frameCount}%` }}
                      onError={() =>
                        setHiddenFrames((current) => {
                          const next = new Set(current);
                          next.add(index);
                          return next;
                        })
                      }
                    />
                  ),
                )}
              </div>
              {cuts.map((cut, index) => {
                const isSelected = selected === index;
                return (
                  <div
                    key={`${cut.start_sec}-${cut.end_sec}-${index}`}
                    className={`cut-block${isSelected ? " selected" : ""}`}
                    style={{
                      left: `${(cut.start_sec / duration) * 100}%`,
                      width: `${Math.max(((cut.end_sec - cut.start_sec) / duration) * 100, 0.4)}%`,
                    }}
                    onPointerDown={(event) => {
                      if ((event.target as HTMLElement).classList.contains("cut-handle")) return;
                      const already = selected === index;
                      setSelected(index);
                      setActiveEdge("end");
                      focusEditor();
                      if (!already) {
                        event.stopPropagation();
                        onActivate?.(cut);
                        return;
                      }
                      const sec = secFromClient(event.clientX);
                      begin(event, {
                        kind: "move",
                        index,
                        originStart: cut.start_sec,
                        originEnd: cut.end_sec,
                        originSec: sec,
                        originX: event.clientX,
                      });
                    }}
                  >
                    {isSelected ? (
                      <>
                        <i
                          className={`cut-handle left${activeEdge === "start" ? " hot" : ""}`}
                          onPointerDown={(event) => {
                            setSelected(index);
                            setActiveEdge("start");
                            begin(event, { kind: "resize", index, edge: "start", originX: event.clientX });
                          }}
                        />
                        <span className="cut-label">
                          {formatSec(cut.start_sec)} – {formatSec(cut.end_sec)}
                        </span>
                        <i
                          className={`cut-handle right${activeEdge === "end" ? " hot" : ""}`}
                          onPointerDown={(event) => {
                            setSelected(index);
                            setActiveEdge("end");
                            begin(event, { kind: "resize", index, edge: "end", originX: event.clientX });
                          }}
                        />
                      </>
                    ) : null}
                  </div>
                );
              })}
              {draft && draft.end_sec - draft.start_sec >= 0.01 ? (
                <div
                  className="cut-block draft"
                  style={{
                    left: `${(draft.start_sec / duration) * 100}%`,
                    width: `${Math.max(((draft.end_sec - draft.start_sec) / duration) * 100, 0.4)}%`,
                  }}
                />
              ) : null}
            </div>
            <div className="cut-playhead" style={{ left: `${(clamp(currentTime, 0, duration) / duration) * 100}%` }}>
              <b
                onPointerDown={(event) => {
                  begin(event, { kind: "playhead", originX: event.clientX });
                  scrub(secFromClient(event.clientX));
                }}
              />
            </div>
          </div>
        </div>
      </div>
      <div className="cut-list">
        <span>待删除 {cuts.length} 段</span>
        {cuts.map((cut, index) => (
          <button
            key={`${cut.start_sec}-${cut.end_sec}-${index}`}
            type="button"
            className={selected === index ? "on" : ""}
            onClick={() => {
              setSelected(index);
              setActiveEdge("end");
              onSeek(cut.start_sec);
              onActivate?.(cut);
              focusEditor();
            }}
          >
            {index + 1}. {formatSec(cut.start_sec)} – {formatSec(cut.end_sec)}
          </button>
        ))}
        <button type="button" onClick={addSegment} disabled={keepGaps(cuts, duration).length === 0} title="在播放头所在的保留区域加一段，大约 1 秒，再用起点和终点改长度">
          添加一段
        </button>
      </div>
      <div className="cut-meta">
        {selectedCut ? (
          <>
            <span>
              删除 {formatSec(selectedCut.start_sec)} – {formatSec(selectedCut.end_sec)}（
              {(selectedCut.end_sec - selectedCut.start_sec).toFixed(2)} 秒）
            </span>
            {caption ? <span>{caption}</span> : null}
            <span className="cut-step">
              <button type="button" onClick={() => { placeEdge("start", selectedCut.start_sec - NUDGE_SEC); focusEditor(); }}>
                −0.1
              </button>
              起点
              <button type="button" onClick={() => { placeEdge("start", selectedCut.start_sec + NUDGE_SEC); focusEditor(); }}>
                +0.1
              </button>
              <button type="button" onClick={() => { placeEdge("start", currentTime); focusEditor(); }}>
                设为起点
              </button>
            </span>
            <span className="cut-step">
              <button type="button" onClick={() => { placeEdge("end", selectedCut.end_sec - NUDGE_SEC); focusEditor(); }}>
                −0.1
              </button>
              终点
              <button type="button" onClick={() => { placeEdge("end", selectedCut.end_sec + NUDGE_SEC); focusEditor(); }}>
                +0.1
              </button>
              <button type="button" onClick={() => { placeEdge("end", currentTime); focusEditor(); }}>
                设为终点
              </button>
            </span>
            <button type="button" onClick={() => restore(selected ?? 0)}>
              恢复这段
            </button>
          </>
        ) : (
          <span>可以同时删多段。点「添加一段」在播放头处再剪一段，或在浅色区域拖出新的一段。</span>
        )}
        <span className="cut-zoom">
          放大
          <Slider
            min={1}
            max={maxZoom}
            step={0.01}
            value={zoomValue}
            onChange={(value) => setZoom(typeof value === "number" ? value : 1)}
            tooltip={{ formatter: (value) => `${Math.round((value ?? 1) * 100)}%` }}
          />
        </span>
      </div>
    </div>
  );
}
