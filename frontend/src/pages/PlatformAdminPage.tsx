import { useEffect, useState } from "react";
import { motion, useSpring } from "framer-motion";
import {
  getPlatformHealth,
  getTenant,
  listTenants,
  setTenantStatus,
  setUserStatus,
  PlatformHealth,
  TenantDetail,
  TenantOverview,
} from "../api/platformAdmin";
import { ApiError } from "../api/client";

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

/** Same animated-count treatment as the tenant-facing Analytics page, for
 * visual consistency between the two "watch what's happening" surfaces. */
function CountUp({ value, format }: { value: number; format?: (n: number) => string }) {
  const spring = useSpring(0, { stiffness: 90, damping: 20 });
  const [display, setDisplay] = useState(0);

  useEffect(() => {
    spring.set(value);
  }, [value, spring]);

  useEffect(() => {
    const unsub = spring.on("change", (v) => setDisplay(v));
    return unsub;
  }, [spring]);

  return <>{format ? format(display) : Math.round(display).toLocaleString()}</>;
}

function StatCard({ label, value, hint, index }: { label: string; value: number; hint?: string; index: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: index * 0.05, ease: "easeOut" }}
      className="metric-card"
    >
      <p className="metric-card-label">{label}</p>
      <p className="metric-card-value text-gray-900">
        <CountUp value={value} />
      </p>
      {hint && <p className="metric-card-hint">{hint}</p>}
    </motion.div>
  );
}

function TenantDrawer({
  tenantId,
  onClose,
  onChanged,
}: {
  tenantId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<TenantDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyUser, setBusyUser] = useState<string | null>(null);

  function load() {
    getTenant(tenantId)
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed to load tenant."));
  }

  useEffect(load, [tenantId]);

  async function toggleUser(userId: string, isActive: boolean) {
    const verb = isActive ? "reactivate" : "suspend";
    if (!window.confirm(`Really ${verb} this user's login?`)) return;
    setBusyUser(userId);
    try {
      await setUserStatus(userId, isActive);
      load();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Failed to ${verb} user.`);
    } finally {
      setBusyUser(null);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="h-full w-full max-w-lg overflow-y-auto bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
          <p className="text-sm font-medium text-gray-700">
            {detail?.overview.name ?? "Tenant"}
          </p>
          <button onClick={onClose} className="text-sm text-gray-400 hover:text-gray-600">
            Close
          </button>
        </div>

        {error && (
          <div role="alert" className="mx-5 mt-4 rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        {!detail ? (
          <div className="text-center py-16 text-sm text-gray-400">Loading…</div>
        ) : (
          <div className="p-5 space-y-6">
            <div className="grid grid-cols-2 gap-3">
              <StatCard index={0} label="Users" value={detail.overview.user_count} />
              <StatCard index={1} label="Assistants" value={detail.overview.assistant_count} />
              <StatCard index={2} label="Tokens (30d)" value={detail.overview.tokens_30d} />
              <StatCard index={3} label="Requests (30d)" value={detail.overview.requests_30d} />
            </div>

            <div className="card card-hover overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100">
                <p className="text-sm font-medium text-gray-700">Users</p>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                  <tr>
                    <th className="text-left font-medium px-3 py-2">Email</th>
                    <th className="text-left font-medium px-3 py-2">Role</th>
                    <th className="text-left font-medium px-3 py-2">Last login</th>
                    <th className="text-right font-medium px-3 py-2">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {detail.users.map((u) => (
                    <tr key={u.user_id}>
                      <td className="px-3 py-2 text-gray-700">{u.email}</td>
                      <td className="px-3 py-2 text-gray-600">{u.role}</td>
                      <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                        {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          disabled={busyUser === u.user_id}
                          onClick={() => toggleUser(u.user_id, !u.is_active)}
                          className={u.is_active ? "badge-live" : "badge-paused"}
                        >
                          {u.is_active ? "active" : "suspended"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {detail.usage_daily.length > 0 && (
              <div className="card card-hover overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-100">
                  <p className="text-sm font-medium text-gray-700">Token usage (30d)</p>
                </div>
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                    <tr>
                      <th className="text-left font-medium px-3 py-2">Day</th>
                      <th className="text-right font-medium px-3 py-2">Tokens</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {[...detail.usage_daily].reverse().map((d) => (
                      <tr key={d.day}>
                        <td className="px-3 py-2 text-gray-700">{d.day}</td>
                        <td className="px-3 py-2 text-right text-gray-700">{d.tokens_used.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function PlatformAdminPage() {
  const [tenants, setTenants] = useState<TenantOverview[]>([]);
  const [health, setHealth] = useState<PlatformHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openTenant, setOpenTenant] = useState<string | null>(null);
  const [busyTenant, setBusyTenant] = useState<string | null>(null);

  function load() {
    Promise.all([listTenants(), getPlatformHealth(30)])
      .then(([t, h]) => {
        setTenants(t);
        setHealth(h);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed to load platform data."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function toggleTenant(tenantId: string, isActive: boolean) {
    const verb = isActive ? "reactivate" : "suspend";
    if (
      !window.confirm(
        isActive
          ? "Reactivate this workspace? Its users will be able to sign in again."
          : "Suspend this workspace? Every user in it will be immediately unable to sign in.",
      )
    )
      return;
    setBusyTenant(tenantId);
    try {
      await setTenantStatus(tenantId, isActive);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Failed to ${verb} workspace.`);
    } finally {
      setBusyTenant(null);
    }
  }

  const totalUsers = tenants.reduce((a, t) => a + t.user_count, 0);
  const totalAssistants = tenants.reduce((a, t) => a + t.assistant_count, 0);
  const totalTokens30d = tenants.reduce((a, t) => a + t.tokens_30d, 0);
  const totalRequests30d = health?.daily.reduce((a, d) => a + d.answers, 0) ?? 0;
  const avgErrorRate =
    health && totalRequests30d > 0
      ? health.daily.reduce((a, d) => a + d.error_rate * d.answers, 0) / totalRequests30d
      : 0;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Super Admin</h1>
          <p className="page-subtitle">
            Every workspace on the platform — usage, activity, and health. No message content, ever — counts and rates only.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="text-center py-16 text-sm text-gray-400">Loading…</div>
      ) : error ? (
        <div role="alert" className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 grid-cols-2 sm:grid-cols-4">
            <StatCard index={0} label="Workspaces" value={tenants.length} />
            <StatCard index={1} label="Users" value={totalUsers} />
            <StatCard index={2} label="Assistants" value={totalAssistants} />
            <StatCard index={3} label="Tokens (30d)" value={totalTokens30d} />
          </div>

          {health && (
            <div className="card card-hover p-5">
              <p className="text-sm font-medium text-gray-700 mb-1">
                Platform health <span className="font-normal text-gray-400">— last 30 days, every tenant</span>
              </p>
              <p className="text-xs text-gray-500 mb-3">
                {totalRequests30d.toLocaleString()} requests · {pct(avgErrorRate)} error rate
              </p>
              <div className="flex flex-wrap gap-3">
                {health.providers.map((p) => (
                  <div key={p.provider ?? "unknown"} className="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2">
                    <span className="badge bg-brand-50 text-brand-700">{p.provider ?? "unknown"}</span>
                    <span className="text-sm text-gray-600">{p.answers} answers</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="card card-hover overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-100">
              <p className="text-sm font-medium text-gray-700">Workspaces</p>
            </div>
            {tenants.length === 0 ? (
              <p className="px-4 py-6 text-sm text-gray-400 text-center">No workspaces yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                    <tr>
                      <th className="text-left font-medium px-3 py-2.5">Workspace</th>
                      <th className="text-left font-medium px-3 py-2.5">Plan</th>
                      <th className="text-right font-medium px-3 py-2.5">Users</th>
                      <th className="text-right font-medium px-3 py-2.5">Assistants</th>
                      <th className="text-right font-medium px-3 py-2.5">Tokens (30d)</th>
                      <th className="text-right font-medium px-3 py-2.5">Requests (30d)</th>
                      <th className="text-right font-medium px-3 py-2.5">Error rate</th>
                      <th className="text-right font-medium px-3 py-2.5">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {tenants.map((t) => (
                      <tr key={t.tenant_id} className="hover:bg-brand-50/40 transition-colors">
                        <td className="px-3 py-2.5">
                          <button
                            className="text-brand-700 hover:underline font-medium"
                            onClick={() => setOpenTenant(t.tenant_id)}
                          >
                            {t.name}
                          </button>
                        </td>
                        <td className="px-3 py-2.5 text-gray-600">{t.plan_tier}</td>
                        <td className="px-3 py-2.5 text-right text-gray-700">{t.user_count}</td>
                        <td className="px-3 py-2.5 text-right text-gray-700">{t.assistant_count}</td>
                        <td className="px-3 py-2.5 text-right text-gray-700">{t.tokens_30d.toLocaleString()}</td>
                        <td className="px-3 py-2.5 text-right text-gray-700">{t.requests_30d}</td>
                        <td className="px-3 py-2.5 text-right text-gray-700">{pct(t.error_rate_30d)}</td>
                        <td className="px-3 py-2.5 text-right">
                          <button
                            disabled={busyTenant === t.tenant_id}
                            onClick={() => toggleTenant(t.tenant_id, !t.is_active)}
                            className={t.is_active ? "badge-live" : "badge-paused"}
                          >
                            {t.is_active ? "active" : "suspended"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {openTenant && (
        <TenantDrawer
          tenantId={openTenant}
          onClose={() => setOpenTenant(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}
