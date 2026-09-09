import { api } from "./client";

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  tenant_id: string;
  user_id: string;
  role: string;
  is_platform_admin?: boolean;
}

export function register(
  tenantName: string,
  email: string,
  password: string,
): Promise<TokenResponse> {
  return api.post<TokenResponse>("/auth/register", {
    tenant_name: tenantName,
    email,
    password,
  });
}

export function login(
  email: string,
  password: string,
): Promise<TokenResponse> {
  return api.post<TokenResponse>("/auth/login", { email, password });
}

/** Sign-in methods this deployment offers beyond email + password. */
export interface AuthProviders {
  google: boolean;
}

export function getAuthProviders(): Promise<AuthProviders> {
  return api.get<AuthProviders>("/auth/providers");
}

export interface ForgotPasswordResult {
  detail: string;
  /** False when this deployment has no email provider configured, so the UI can
   * say the link could not actually be sent instead of implying it was. */
  email_sent: boolean;
}

export function forgotPassword(email: string): Promise<ForgotPasswordResult> {
  return api.post<ForgotPasswordResult>("/auth/forgot-password", { email });
}

/** Returns a normal token pair — a successful reset signs you straight in. */
export function resetPassword(token: string, newPassword: string): Promise<TokenResponse> {
  return api.post<TokenResponse>("/auth/reset-password", {
    token,
    new_password: newPassword,
  });
}
