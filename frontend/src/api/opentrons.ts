// Client for the liquid-handler jog API (backend/app/api/liquid_handler.py).
//
// Relative jogging only. This machine has no working endstops, so there is no
// absolute datum and therefore no move-to endpoint to call.
//
// Every command answers with fresh driver status, so callers never need a
// follow-up GET. Transport failures are folded into the same {ok, detail} shape
// the backend uses, so the UI has exactly one error path to render.

export interface LhStatus {
  state?: string;
  connected?: boolean;
  homed?: boolean;
  reference_lost?: boolean;
  z_below_datum_mm?: number;
  endstops_functional?: boolean;
  error?: string;
}

export interface LhResult {
  ok: boolean;
  detail: string;
  duration_s?: number | null;
  status?: LhStatus | null;
}

export interface LhLimits {
  joggable_axes: string[];
  refused_axes: string[];
  max_step_mm: number;
  relative_only: boolean;
  endstops_functional: boolean;
  note: string;
}

const base = (id: string) => `/api/liquid-handlers/${encodeURIComponent(id)}`;

async function post(url: string, body?: unknown): Promise<LhResult> {
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (r.status === 409) {
      return { ok: false, detail: "busy: a command is already running" };
    }
    return (await r.json()) as LhResult;
  } catch (e) {
    return { ok: false, detail: `request failed: ${String(e)}` };
  }
}

export function jog(
  id: string,
  axis: string,
  delta: number,
  feedrate?: number,
): Promise<LhResult> {
  return post(`${base(id)}/jog`, { axis, delta, feedrate: feedrate ?? null });
}

export function home(id: string): Promise<LhResult> {
  return post(`${base(id)}/home`);
}

export function stop(id: string): Promise<LhResult> {
  return post(`${base(id)}/stop`);
}

export async function getState(id: string): Promise<LhResult> {
  try {
    const r = await fetch(`${base(id)}/state`);
    return (await r.json()) as LhResult;
  } catch (e) {
    return { ok: false, detail: `request failed: ${String(e)}` };
  }
}

export async function getLimits(id: string): Promise<LhLimits | null> {
  try {
    const r = await fetch(`${base(id)}/limits`);
    if (!r.ok) return null;
    return (await r.json()) as LhLimits;
  } catch {
    return null;
  }
}
