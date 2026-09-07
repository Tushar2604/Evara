import { api } from "./client";

export type PlanTier = "free" | "starter" | "growth" | "scale";

export interface Plan {
  tier: PlanTier;
  name: string;
  price_usd: number;
  tagline: string;
  /** null = unlimited, the headline of every paid tier. */
  max_assistants: number | null;
  max_documents: number;
  daily_token_quota: number;
  monthly_api_calls: number;
  scopes: string[];
  /** Can this plan mint API keys at all? False on Free. */
  api_access: boolean;
  /** API consumption — running an assistant as an agent. Scale only. */
  agent_api: boolean;
}

export interface PlanUsage {
  assistants_used: number;
  /** null = unlimited. */
  assistants_remaining: number | null;
  documents_used: number;
  tokens_used_today: number;
  api_calls_this_period: number;
  api_calls_remaining: number;
}

export interface Subscription {
  tier: PlanTier;
  status: string;
  started_at: string;
  current_period_end: string | null;
  auto_renew: boolean;
  canceled_at: string | null;
}

export interface BillingOverview {
  subscription: Subscription;
  plan: Plan;
  usage: PlanUsage;
  available_plans: Plan[];
}

export interface BillingTransaction {
  id: string;
  kind: string;
  amount_usd: number;
  description: string;
  plan_tier: string | null;
  reference: string | null;
  created_at: string;
}

export function getBilling(): Promise<BillingOverview> {
  return api.get<BillingOverview>("/billing");
}

export function changePlan(tier: PlanTier): Promise<BillingOverview> {
  return api.post<BillingOverview>("/billing/subscription", { tier });
}

export function cancelPlan(): Promise<BillingOverview> {
  return api.delete<BillingOverview>("/billing/subscription");
}

export function getTransactions(): Promise<BillingTransaction[]> {
  return api.get<BillingTransaction[]>("/billing/transactions");
}
