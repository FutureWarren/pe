"use client";

import { useEffect, useRef, useState } from "react";

/** Animate a numeric value toward its target over ~500ms (ease-out-quart).
 *  Renders the intermediate integers; jumps instantly under reduced motion.
 *  Pair the output with `tabular-nums` so digit changes don't jitter width. */
export function useCountUp(target: number, durationMs = 500): number {
  const [display, setDisplay] = useState(target);
  const fromRef = useRef(target);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      fromRef.current = target;
      setDisplay(target);
      return;
    }
    const from = fromRef.current;
    if (from === target) return;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 4); // ease-out-quart
      const value = from + (target - from) * eased;
      setDisplay(t < 1 ? Math.round(value) : target);
      if (t < 1) {
        frameRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = target;
      }
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      fromRef.current = target;
    };
  }, [target, durationMs]);

  return display;
}
