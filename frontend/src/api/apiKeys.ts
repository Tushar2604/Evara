import { api } from "./client";

export interface ApiKey {
  id: string;
  name: string;
  /** First characters of the key — identifies a row without exposing it. */
  prefix: string;
  /** What the plan granted when the key was minted. */
  scopes: string[];
  /**
   * What it can do today: `scopes` intersected with the live plan. Differs
   * from `scopes` after a downgrade, and the UI shows that difference rather
   * than letting someone believe a key still works.
   */
  active_scopes: string[];
  plan_tier: string;
  is_active: boolean;
  last_used_at: string | null;
  created_at: string;
}

export interface ApiKeyList {
  keys: ApiKey[];
  plan_tier: string;
  api_access: boolean;
  agent_api: boolean;
  available_scopes: string[];
}

export interface CreatedApiKey {
  key: ApiKey;
  /** The only time the plaintext key is ever returned. */
  raw_key: string;
}

export function getApiKeys(): Promise<ApiKeyList> {
  return api.get<ApiKeyList>("/api-keys");
}

export function createApiKey(name: string, scopes?: string[]): Promise<CreatedApiKey> {
  return api.post<CreatedApiKey>("/api-keys", { name, scopes });
}

export function revokeApiKey(id: string): Promise<void> {
  return api.delete<void>(`/api-keys/${id}`);
}

/** Human labels for the scope vocabulary the backend defines. */
export const SCOPE_LABEL: Record<string, string> = {
  "assistants.read": "Read assistants",
  "assistants.write": "Create & edit assistants",
  "knowledge.write": "Upload knowledge",
  "analytics.read": "Read analytics",
  "agent.run": "Run assistants as agents",
};
