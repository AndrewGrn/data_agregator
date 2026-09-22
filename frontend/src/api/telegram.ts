import { apiGet, apiPost } from "../api";

export type OnboardingStep = "idle" | "queued" | "resolving" | "joining" | "joined" | "failed";
export type TargetKind = "channel" | "group" | "private" | null;

export interface TelegramTargetRow {
  id: number;
  name: string;
  identifier: string;
  kind: TargetKind;
  is_active: boolean;
  onboarding_status: "ready" | "needs_account" | "blocked";
  onboarding_step: OnboardingStep;
  onboarding_error: string | null;
  account: { id: number; label: string; alive: boolean | null } | null;
  events_count: number;
  last_event_at: string | null;
}

export interface TelegramAccountRow {
  id: number;
  label: string;
  username: string | null;
  pool_mode: "shared" | "dedicated";
  is_active: boolean;
  alive: boolean | null;
  last_checked_at: string | null;
  dead_reason: string | null;
  health_score: number;
  cooldown_until: string | null;
  targets_count: number;
  joins_today: number;
  join_daily_limit: number;
}

export interface TelegramJobRow {
  id: number;
  target_id: number;
  target_name: string;
  account_id: number | null;
  account_label: string;
  status: string;
  job_type: string;
  attempt: number;
  error: string | null;
  wait_reason: string;
  updated_at: string | null;
  updated_at_text: string;
  error_at_text: string;
}

const BASE = "/api/modules/telegram";

export function fetchTelegramModule() {
  return apiGet<{ targets: TelegramTargetRow[]; jobs: TelegramJobRow[]; [k: string]: unknown }>(BASE);
}

export function onboardTelegramTarget(input: string, opts: { account_id?: number; allow_join?: boolean } = {}) {
  return apiPost<TelegramTargetRow>(`${BASE}/onboard`, { input, ...opts });
}

export function fetchTelegramAccounts() {
  return apiGet<TelegramAccountRow[]>(`${BASE}/accounts/overview`);
}

export function checkTelegramAccount(id: number) {
  return apiPost<{ alive: boolean; error: string | null; checked_at: string }>(`${BASE}/accounts/${id}/check-alive`);
}

export function setTelegramAccountPoolMode(id: number, pool_mode: "shared" | "dedicated") {
  return apiPost<TelegramAccountRow>(`${BASE}/accounts/${id}/pool-mode`, { pool_mode });
}

export function fetchTelegramAccountTargets(id: number) {
  return apiGet<TelegramTargetRow[]>(`${BASE}/accounts/${id}/targets`);
}

export function reassignTelegramTarget(id: number, account_id?: number) {
  return apiPost<TelegramTargetRow>(`${BASE}/targets/${id}/reassign`, account_id ? { account_id } : {});
}

export function retryTelegramOnboarding(id: number) {
  return apiPost<TelegramTargetRow>(`${BASE}/targets/${id}/retry-onboarding`);
}

// Real contract of POST /modules/telegram/targets/{id}/update (app/routers/api.py:2571):
// accepts config fields (name, identifier, limit, poll_interval_seconds, backfill_*, comments_*, is_risky...),
// NOT is_active/run_now/delete as the original brief guessed. Kept for completeness/future settings UI,
// but pause/resume/run-now/delete are wired through the endpoints below instead.
export function updateTelegramTarget(id: number, body: Record<string, unknown>) {
  return apiPost<unknown>(`${BASE}/targets/${id}/update`, body);
}

// Generic per-module target lifecycle endpoints (app/routers/api.py:2145-2206) — these are what
// pause/resume/backfill-now actually call.
export function stopTelegramTarget(id: number) {
  return apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${id}/stop`);
}

export function startTelegramTarget(id: number) {
  return apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${id}/start`);
}

export function runTelegramTargetNow(id: number) {
  return apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${id}/run-now`);
}

// Account connect flow (unchanged bodies, moved out of the old page).
export interface TelegramAuthPayload {
  api_id: string;
  api_hash: string;
  phone: string;
  temp_session_string: string;
  phone_code_hash: string;
}

export function startTelegramAccountAuth(payload: { api_id: string; api_hash: string; phone: string }) {
  return apiPost<{ ok: boolean; auth_payload: TelegramAuthPayload }>(`${BASE}/accounts/start-auth`, payload);
}

export function completeTelegramAccountAuth(payload: {
  label: string;
  hourly_limit: number;
  api_id: string;
  api_hash: string;
  phone: string;
  temp_session_string: string;
  phone_code_hash: string;
  code: string;
  password: string;
}) {
  return apiPost<{ ok: boolean; account_id: number; label: string }>(`${BASE}/accounts/complete-auth`, payload);
}

// Service actions (menu on the Settings/Accounts tab).
export function syncTelegramMemberships() {
  return apiPost<unknown>("/api/telegram/sync-memberships");
}

export function resetTelegramAccountsCooldown() {
  return apiPost<{ ok: boolean; updated: number }>(`${BASE}/accounts/reset-cooldown`);
}

// Jobs tab.
export function retryTelegramJobsFailed(target_id?: number) {
  return apiPost<{ ok: boolean; updated: number }>(`${BASE}/jobs/retry-failed`, target_id ? { target_id } : undefined);
}

export function retryTelegramJob(jobId: number) {
  return apiPost<{ ok: boolean }>(`${BASE}/jobs/${jobId}/retry`);
}
