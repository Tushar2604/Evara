import { api } from "./client";

export interface TenantOverview {
  tenant_id: string;
  name: string;
  slug: string;
  is_active: boolean;
  created_at: string;
  plan_tier: string;
  subscription_status: string;
  user_count: number;
  assistant_count: number;
  tokens_today: number;
  tokens_30d: number;
  requests_30d: number;
  error_rate_30d: number;
  refusal_rate_30d: number;
}

export interface PlatformUser {
  user_id: string;
  email: string;
  role: string;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface UsageDay {
  day: string;
  tokens_used: number;
}

export interface TenantDetail {
  overview: TenantOverview;
  users: PlatformUser[];
  usage_daily: UsageDay[];
}

export interface PlatformHealthDay {
  day: string;
  answers: number;
  error_rate: number;
  refusal_rate: number;
  avg_latency_ms: number;
}

export interface PlatformProviderStat {
  provider: string | null;
  answers: number;
  avg_top_score: number | null;
  avg_tokens: number;
}

export interface PlatformHealth {
  days: number;
  daily: PlatformHealthDay[];
  providers: PlatformProviderStat[];
}

export function listTenants(): Promise<TenantOverview[]> {
  return api.get<TenantOverview[]>("/platform-admin/tenants");
}

export function getTenant(tenantId: string): Promise<TenantDetail> {
  return api.get<TenantDetail>(`/platform-admin/tenants/${tenantId}`);
}

export function setTenantStatus(
  tenantId: string,
  isActive: boolean,
): Promise<TenantOverview> {
  return api.post<TenantOverview>(`/platform-admin/tenants/${tenantId}/status`, {
    is_active: isActive,
  });
}

export function setUserStatus(
  userId: string,
  isActive: boolean,
): Promise<{ is_active: boolean }> {
  return api.post<{ is_active: boolean }>(`/platform-admin/users/${userId}/status`, {
    is_active: isActive,
  });
}

export function getPlatformHealth(days = 30): Promise<PlatformHealth> {
  return api.get<PlatformHealth>(`/platform-admin/health?days=${days}`);
}
