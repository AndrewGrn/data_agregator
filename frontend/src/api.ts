export type Role = "admin" | "user";

export type User = {
  id: number;
  username: string;
  is_admin: boolean;
  role: Role;
  is_active?: boolean;
  totp_confirmed?: boolean;
};

export type AdminRegistrationToken = {
  id: number;
  label: string | null;
  is_active: boolean;
  max_uses: number;
  used_count: number;
  created_by_user_id: number | null;
  created_by_username: string | null;
  used_by_user_id: number | null;
  used_by_username: string | null;
  expires_at: string | null;
  created_at: string | null;
  is_expired: boolean;
};

export type AdminUserRow = {
  id: number;
  username: string;
  role: Role;
  is_admin: boolean;
  is_active: boolean;
  totp_enabled: boolean;
  totp_confirmed: boolean;
  created_by_token_id: number | null;
  created_at: string | null;
};

export type AdminResources = {
  accounts: Array<{
    id: number;
    label: string;
    parser_type: string;
    owner_user_id: number | null;
    owner_username: string | null;
    is_active: boolean;
  }>;
  targets: Array<{
    id: number;
    name: string;
    identifier: string;
    parser_type: string;
    owner_user_id: number | null;
    owner_username: string | null;
    is_active: boolean;
  }>;
  links: Array<{
    id: number;
    target_id: number;
    account_id: number;
    owner_user_id: number | null;
    owner_username: string | null;
    is_active: boolean;
  }>;
};

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = "Помилка запиту";
    try {
      const payload = await response.json();
      detail = String(payload?.detail ?? payload?.message ?? detail);
    } catch {
      // ignore
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    credentials: "include"
  });
  return parseResponse<T>(response);
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json"
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  return parseResponse<T>(response);
}

export async function getMe(): Promise<User> {
  return apiGet<User>("/api/auth/me");
}

export async function login(username: string, password: string, otpCode: string): Promise<User> {
  const payload = await apiPost<{ ok: boolean; user: User }>("/api/auth/login", {
    username,
    password,
    otp_code: otpCode
  });
  return payload.user;
}

export async function logout(): Promise<void> {
  await apiPost<{ ok: boolean }>("/api/auth/logout");
}

export async function registerWithInvite(inviteToken: string, username: string, password: string) {
  return apiPost<{ ok: boolean; username: string; otp_secret: string; otp_uri: string; message: string }>(
    "/api/auth/register",
    {
      invite_token: inviteToken,
      username,
      password
    }
  );
}

export async function start2FASetup(username: string, password: string) {
  return apiPost<{ ok: boolean; username: string; otp_secret: string; otp_uri: string; already_confirmed: boolean }>(
    "/api/auth/setup-2fa/start",
    {
      username,
      password
    }
  );
}

export async function confirm2FASetup(username: string, password: string, otpCode: string) {
  return apiPost<{ ok: boolean }>("/api/auth/setup-2fa/confirm", {
    username,
    password,
    otp_code: otpCode
  });
}

export async function adminCreateRegistrationToken(label: string, maxUses: number, expiresInHours: number) {
  return apiPost<{ ok: boolean; id: number; token: string; label: string | null; max_uses: number; expires_at: string | null }>(
    "/api/admin/registration-tokens",
    {
      label,
      max_uses: maxUses,
      expires_in_hours: expiresInHours
    }
  );
}

export async function adminListRegistrationTokens() {
  return apiGet<AdminRegistrationToken[]>("/api/admin/registration-tokens");
}

export async function adminDeactivateRegistrationToken(tokenId: number) {
  return apiPost<{ ok: boolean }>(`/api/admin/registration-tokens/${tokenId}/deactivate`);
}

export async function adminListUsers() {
  return apiGet<AdminUserRow[]>("/api/admin/users");
}

export async function adminUpdateUser(userId: number, payload: { role?: Role; is_active?: boolean }) {
  return apiPost<{ ok: boolean }>(`/api/admin/users/${userId}`, payload);
}

export async function adminGetResources() {
  return apiGet<AdminResources>("/api/admin/resources");
}
