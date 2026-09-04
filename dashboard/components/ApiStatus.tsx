"use client";

import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";

type State = "waking" | "up" | "down";

/**
 * Render's free tier sleeps after 15 minutes idle and takes ~50s to wake. Without this
 * the first load of a cold demo looks broken rather than asleep, which is a worse
 * first impression than a slow one.
 */
export function ApiStatus() {
  const [state, setState] = useState<State>("waking");
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;
    let attempt = 0;
    const poll = async () => {
      try {
        const result = await api.health();
        if (!cancelled) {
          setHealth(result);
          setState("up");
        }
      } catch {
        attempt += 1;
        if (cancelled) return;
        // Up to ~75s of patience, which covers a free-tier cold start.
        if (attempt > 25) setState("down");
        else setTimeout(poll, 3000);
      }
    };
    poll();
    return () => {
      cancelled = true;
    };
  }, []);

  const dot =
    state === "up" ? "bg-tri-minor" : state === "waking" ? "bg-tri-delayed" : "bg-tri-immediate";

  return (
    <div className="flex items-center gap-2 text-xs text-fg-dim">
      <span
        className={`inline-block h-2 w-2 rounded-full ${dot} ${
          state === "waking" ? "animate-pulse" : ""
        }`}
      />
      {state === "up" && health && (
        <span className="font-mono">
          {health.policy_codes_loaded} codes · v{health.policy_version}
          {health.read_only && " · read-only"}
        </span>
      )}
      {state === "waking" && <span>waking the API — free tier, ~50s</span>}
      {state === "down" && <span>API unreachable</span>}
    </div>
  );
}
