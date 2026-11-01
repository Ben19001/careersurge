// The local API (backend/app/local_api.py). Same origin as this page, so URLs are relative. Every
// /api/ call carries the per-run token the launcher put in the window URL's fragment: read once,
// kept in sessionStorage (a reload keeps working), and removed from the address bar.
const TOKEN_KEY = "careersurge-token";
const TOKEN_HEADER = "X-CareerSurge-Token";

export function initToken(): void {
  const match = /(?:^|[#&])token=([^&]+)/.exec(window.location.hash);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

export function hasToken(): boolean {
  return sessionStorage.getItem(TOKEN_KEY) !== null;
}

export class ApiError extends Error {
  status: number;
  // The whole error body: a refused Run now carries `reason` and `detail` beside `error`.
  payload: Record<string, unknown> | null;
  constructor(message: string, status: number, payload: Record<string, unknown> | null = null) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set(TOKEN_HEADER, sessionStorage.getItem(TOKEN_KEY) ?? "");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(payload?.error ?? response.statusText, response.status, payload);
  }
  return response;
}

// GET without a body, otherwise a JSON body sent as `method` (POST unless the call says PUT).
export async function api<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const init: RequestInit =
    body === undefined
      ? {}
      : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  return (await send(path, init)).json();
}

export async function apiUpload<T>(path: string, form: FormData, method = "POST"): Promise<T> {
  return (await send(path, { method, body: form })).json();
}

// An <img src> cannot send the token header, so the screenshot is fetched and shown from a blob URL.
export async function apiBlobUrl(path: string): Promise<string> {
  return URL.createObjectURL(await (await send(path)).blob());
}

export type Pay = { min: number | null; max: number | null; currency: string | null };

export type RepostMatch = {
  kind: "strong" | "weak";
  title: string | null;
  company: string | null;
  url: string | null;
  first_seen_at: string | null;
};

export type Job = {
  id: number;
  ats: string;
  title: string | null;
  company: string | null;
  location: string | null;
  url: string;
  pay: Pay | null;
  repost_match: RepostMatch | null;
};

export type Inputs = {
  profile: Record<string, string | number | null>;
  resume: { path: string; name: string; sha256: string } | null;
  global_answers: { question: string; answer: string }[];
  consent: Record<string, number>;
};

export type Batch = {
  jobs: Job[];
  inputs: Inputs | null;
  eta: { seconds_per_application: number; queued: Record<string, number> };
};

export type QueueState = {
  state: "paused" | "running" | "backed_off" | "cooling" | "idle";
  paused_reason: "user" | "blocked" | null;
  next_at: number | null;
  pending: number;
};

// Why a job check did not run, from `SourcingRefused`: `detail` holds the numbers (or `message`).
export type Refusal = { reason: string; detail: Record<string, unknown>; at?: string };

export type Progress = {
  queues: Record<string, QueueState>;
  counts: Record<string, number>;
  sourcing_refusal: (Refusal & { at: string }) | null;
  // The newest job check. The counts and `ingested_at` are null until it ends.
  sourcing_run: {
    status: "started" | "succeeded" | "failed";
    started_at: string;
    ingested_at: string | null;
    rows_fetched: number | null;
    rows_skipped: number | null;
    cost_usd: number | null;
  } | null;
  server_time: number;
};

// One row of `GET /api/failed`: a failed application with its listing.
export type FailedApplication = {
  id: number;
  title: string | null;
  company: string | null;
  url: string | null;
  skip_reason: "listing_closed" | "unsupported_form" | "timeout" | "error" | null;
  error: string | null;
  attempt_count: number;
  updated_at: string;
};

export type FieldShape = {
  kind: "text" | "single" | "multi" | "file";
  options: string[];
  max_length: number | null;
};

export type BlockedQuestion = {
  qna_id: number;
  question: string;
  question_type: string;
  scope: "global" | "company" | "job";
  field_shape: FieldShape;
};

export type Blocked = {
  id: number;
  title: string | null;
  company: string | null;
  url: string | null;
  location: string | null;
  ats: string;
  has_company: boolean;
  responsibilities: string | null;
  requirements: string | null;
  key_skills: string[];
  blocked_reason: "question" | "otp" | "captcha" | "verify_submit" | "attempts_exhausted";
  questions: BlockedQuestion[];
  error: string | null;
  has_screenshot: boolean;
  updated_at: string;
  email: string | null;
  code_wait_s?: number;
  // What the app would have sent, for a captcha card and a manual-apply card to copy from.
  answers: { label: string; value: string; is_file: boolean }[];
};

// The reply of the routes that open a posting in the user's own browser; `opened` is false when the machine has none.
export type OpenedPosting = { ok: true; url: string; opened: boolean };
