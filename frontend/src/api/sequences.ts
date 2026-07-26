// Client for the sequence builder (backend/app/api/sequences.py).
//
// Fleet-level, not per-arm: a sequence may hand a tube from one arm to the other.

export type SequenceAction = "move" | "grip" | "ungrip" | "unscrew";

export interface SequenceStep {
  action: SequenceAction;
  device: string;
  pose: string;
  width: number | null;
  half_turns: number;
  speed: number | null;
  note: string;
}

export interface Sequence {
  name: string;
  steps: SequenceStep[];
  note: string;
  updated_at: string;
}

export interface StepResult {
  index: number;
  phase: string; // preflight | started | done | failed | complete
  ok: boolean;
  detail: string;
}

export interface RunResult {
  ok: boolean;
  events: StepResult[];
  problems: string[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const listSequences = () => request<Sequence[]>("/api/sequences");
export const saveSequence = (name: string, seq: Sequence) =>
  request<Sequence>(`/api/sequences/${enc(name)}`, {
    method: "PUT",
    body: JSON.stringify(seq),
  });
export const deleteSequence = (name: string) =>
  request<Sequence[]>(`/api/sequences/${enc(name)}`, { method: "DELETE" });
export const preflightSequence = (name: string) =>
  request<RunResult>(`/api/sequences/${enc(name)}/preflight`, { method: "POST" });
/** Run one step, 1-based — the walkthrough. */
export const runSequenceStep = (name: string, index: number) =>
  request<RunResult>(`/api/sequences/${enc(name)}/step?index=${index}`, { method: "POST" });
export const runSequence = (name: string) =>
  request<RunResult>(`/api/sequences/${enc(name)}/run`, { method: "POST" });

export const emptyStep = (device: string): SequenceStep => ({
  action: "move",
  device,
  pose: "",
  width: null,
  half_turns: 2,
  speed: null,
  note: "",
});
