// Billing: what this workspace is on, what it has used, and what else exists.
//
// The page is arranged around the one question someone opens it to answer —
// "am I about to hit a wall?" — so usage sits above the pricing table rather
// than below it. The plan cards are the answer to the follow-up, and the
// current plan is marked rather than hidden so the comparison is possible at
// all.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CreditCard, Check, Zap, KeyRound } from "lucide-react";
import {
  BillingOverview,
  BillingTransaction,
  Plan,
  PlanTier,
  cancelPlan,
  changePlan,
  getBilling,
  getTransactions,
} from "../api/billing";
import { ApiError } from "../api/client";

/** Unlimited is a word, not a missing number. */
function limitLabel(value: number | null): string {
  return value === null ? "Unlimited" : value.toLocaleString();
}

function UsageStat({
  label,
  used,
  limit,
  suffix,
}: {
  label: string;
  used: number;
  /** null = unlimited, so there is no bar to draw. */
  limit: number | null;
  suffix?: string;
}) {
  const unlimited = limit === null;
  const pct = unlimited ? 0 : Math.min(100, Math.round((used / Math.max(1, limit)) * 100));
  // Amber before red: the point of the bar is to be noticed *before* the wall.
  const tone = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-teal-500";

  return (
    <div className="flex-1 min-w-[180px]">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-lg font-semibold text-gray-900 mt-0.5">
        {used.toLocaleString()}
        {!unlimited && (
          <span className="text-sm font-normal text-gray-400"> / {limit.toLocaleString()}</span>
        )}
        {suffix && <span className="text-sm font-normal text-gray-400"> {suffix}</span>}
      </p>
      <div className="h-1.5 rounded-full bg-gray-100 mt-2 overflow-hidden">
        {unlimited ? (
          <div className="h-full w-full bg-teal-100" />
        ) : (
          <div className={`h-full ${tone} transition-all`} style={{ width: `${pct}%` }} />
        )}
      </div>
      {unlimited && <p className="text-xs text-gray-400 mt-1">Unlimited on this plan</p>}
    </div>
  );
}

function PlanCard({
  plan,
  current,
  busy,
  onChoose,
}: {
  plan: Plan;
  current: boolean;
  busy: boolean;
  onChoose: (tier: PlanTier) => void;
}) {
  return (
    <div
      className={`card p-5 flex flex-col ${
        current ? "ring-2 ring-teal-500" : ""
      }`}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm font-semibold text-gray-900">{plan.name}</p>
          <p className="text-2xl font-semibold text-gray-900 mt-1">
            ${plan.price_usd.toFixed(0)}
            <span className="text-sm font-normal text-gray-400">/mo</span>
          </p>
        </div>
        {current && <span className="badge badge-live">Current</span>}
      </div>

      <p className="text-xs text-gray-500 mt-3 min-h-[48px]">{plan.tagline}</p>

      <ul className="mt-4 space-y-2 text-sm text-gray-600 flex-1">
        <li className="flex items-start gap-2">
          <Check className="w-4 h-4 text-teal-600 shrink-0 mt-0.5" />
          {limitLabel(plan.max_assistants)} assistant
          {plan.max_assistants === 1 ? "" : "s"}
        </li>
        <li className="flex items-start gap-2">
          <Check className="w-4 h-4 text-teal-600 shrink-0 mt-0.5" />
          {plan.max_documents.toLocaleString()} knowledge files
        </li>
        <li className="flex items-start gap-2">
          <Check className="w-4 h-4 text-teal-600 shrink-0 mt-0.5" />
          {(plan.daily_token_quota / 1_000_000).toLocaleString()}M tokens/day
        </li>
        <li className="flex items-start gap-2">
          {plan.api_access ? (
            <Check className="w-4 h-4 text-teal-600 shrink-0 mt-0.5" />
          ) : (
            <span className="w-4 h-4 shrink-0 mt-0.5 text-gray-300 text-center">—</span>
          )}
          <span className={plan.api_access ? "" : "text-gray-400"}>
            {plan.api_access
              ? `API keys · ${plan.monthly_api_calls.toLocaleString()} calls/mo`
              : "No API access"}
          </span>
        </li>
        <li className="flex items-start gap-2">
          {plan.agent_api ? (
            <Zap className="w-4 h-4 text-teal-600 shrink-0 mt-0.5" />
          ) : (
            <span className="w-4 h-4 shrink-0 mt-0.5 text-gray-300 text-center">—</span>
          )}
          <span className={plan.agent_api ? "font-medium text-gray-900" : "text-gray-400"}>
            {plan.agent_api
              ? "API consumption — run assistants as agents"
              : "No agent API"}
          </span>
        </li>
      </ul>

      <button
        disabled={current || busy}
        onClick={() => onChoose(plan.tier)}
        className={`mt-5 w-full ${current ? "btn-secondary" : "btn-primary"}`}
      >
        {current ? "Current plan" : busy ? "Working…" : `Switch to ${plan.name}`}
      </button>
    </div>
  );
}

export default function BillingPage() {
  const [overview, setOverview] = useState<BillingOverview | null>(null);
  const [history, setHistory] = useState<BillingTransaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getBilling(), getTransactions().catch(() => [])])
      .then(([o, t]) => {
        setOverview(o);
        setHistory(t);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load billing."),
      )
      .finally(() => setLoading(false));
  }, []);

  async function choose(tier: PlanTier) {
    setBusy(true);
    setError(null);
    try {
      setOverview(await changePlan(tier));
      setHistory(await getTransactions().catch(() => history));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the plan.");
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    setError(null);
    try {
      setOverview(await cancelPlan());
      setHistory(await getTransactions().catch(() => history));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not cancel.");
    } finally {
      setBusy(false);
    }
  }

  const plan = overview?.plan;
  const usage = overview?.usage;
  const sub = overview?.subscription;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Billing</h1>
          <p className="text-sm text-gray-500 mt-1">Your plan, usage, and invoices.</p>
        </div>
        <Link to="/api-keys" className="btn-secondary">
          <KeyRound className="w-4 h-4" />
          API keys
        </Link>
      </div>

      {error && (
        <div role="alert" className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700 mb-6">
          {error}
        </div>
      )}

      {loading ? (
        <div className="card p-6 space-y-3">
          <div className="skeleton h-6 w-40" />
          <div className="skeleton h-5 w-full" />
        </div>
      ) : (
        overview && plan && usage && sub && (
          <>
            {/* Current plan + usage. First, because "am I about to hit a
                wall" is the question that brings people to this page. */}
            <div className="card p-6 mb-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="flex items-center gap-3">
                  <CreditCard className="w-5 h-5 text-gray-400" />
                  <div>
                    <p className="text-sm font-semibold text-gray-900">
                      {plan.name} plan
                      {plan.price_usd > 0 && (
                        <span className="text-gray-400 font-normal">
                          {" "}· ${plan.price_usd.toFixed(2)}/mo
                        </span>
                      )}
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      {sub.canceled_at
                        ? `Canceled — access continues until ${
                            sub.current_period_end
                              ? new Date(sub.current_period_end).toLocaleDateString()
                              : "the period ends"
                          }`
                        : sub.current_period_end
                          ? `Renews ${new Date(sub.current_period_end).toLocaleDateString()}`
                          : "No renewal — the free plan never lapses"}
                    </p>
                  </div>
                </div>
                {plan.price_usd > 0 && !sub.canceled_at && (
                  <button onClick={cancel} disabled={busy} className="btn-secondary">
                    Cancel plan
                  </button>
                )}
              </div>

              <div className="flex flex-wrap gap-6 mt-6 pt-5 border-t border-gray-100">
                <UsageStat
                  label="Assistants"
                  used={usage.assistants_used}
                  limit={plan.max_assistants}
                />
                <UsageStat
                  label="Knowledge files"
                  used={usage.documents_used}
                  limit={plan.max_documents}
                />
                <UsageStat
                  label="Tokens today"
                  used={usage.tokens_used_today}
                  limit={plan.daily_token_quota}
                />
                <UsageStat
                  label="API calls this period"
                  used={usage.api_calls_this_period}
                  limit={plan.monthly_api_calls}
                />
              </div>
            </div>

            <h2 className="text-sm font-semibold text-gray-900 mb-3">Plans</h2>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4 mb-8">
              {overview.available_plans.map((p) => (
                <PlanCard
                  key={p.tier}
                  plan={p}
                  current={p.tier === sub.tier}
                  busy={busy}
                  onChoose={choose}
                />
              ))}
            </div>

            <div className="card overflow-hidden">
              <div className="px-5 py-3 border-b border-gray-100">
                <p className="text-sm font-semibold text-gray-900">Billing history</p>
              </div>
              {history.length === 0 ? (
                <p className="px-5 py-6 text-sm text-gray-500">Nothing billed yet.</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Description</th>
                      <th>Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((t) => (
                      <tr key={t.id}>
                        <td className="text-gray-500 text-xs">
                          {new Date(t.created_at).toLocaleDateString([], {
                            month: "short",
                            day: "numeric",
                            year: "numeric",
                          })}
                        </td>
                        <td className="text-gray-900">{t.description}</td>
                        <td className="text-gray-600">${t.amount_usd.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )
      )}
    </div>
  );
}
