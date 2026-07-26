// `GET /api/logs` — the one read path onto the log file (R-LOG-4 / R-UI-12, R-UI-8).
//
// Polled with a cursor, no websocket: that is the backend's shape (`backend/app/api/logs.py`),
// and the same endpoint answers both questions the frontend has —
//   * the Logs tab: newest page, then "what was appended since my cursor";
//   * an expanded action: `?run_id=&aid=` once, no cursor.

/** One record as it was written. The log is structured but its keys are not a frozen model,
 *  so this is deliberately open — a renderer that only knows five keys must not drop the
 *  rest. `ts`/`level`/`msg` are the ones every record has. */
export interface LogRecord {
  ts?: string;
  level?: string;
  logger?: string;
  msg?: string;
  event?: string;
  run_id?: string;
  aid?: number;
  device?: string;
  [key: string]: unknown;
}

export interface LogsResponse {
  records: LogRecord[];
  cursor: string;
  /** Not an error: the file rotated or was truncated, so records between the caller's
   *  cursor and this page are gone. Say "log rotated", never render a silent gap. */
  reset: boolean;
  scanned: number;
  truncated: boolean;
  path: string;
}

export interface LogQuery {
  cursor?: string;
  limit?: number;
  run_id?: string;
  /** The stable action identity, not the display index. */
  aid?: number;
  /** A *minimum* severity, not an equality test. */
  level?: string;
  event?: string;
  logger?: string;
  device?: string;
  contains?: string;
}

export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;

export async function fetchLogs(query: LogQuery = {}): Promise<LogsResponse> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const res = await fetch(`/api/logs?${params.toString()}`);
  if (!res.ok) throw new Error(`GET /api/logs failed: HTTP ${res.status}`);
  return (await res.json()) as LogsResponse;
}

/** The records one action produced (R-UI-8). `aid` is enough; `run_id` scopes it to this run
 *  so a re-run of the same plan does not show the previous run's records. */
export const fetchActionLogs = (runId: string, aid: number, limit = 200) =>
  fetchLogs({ run_id: runId || undefined, aid, limit });
