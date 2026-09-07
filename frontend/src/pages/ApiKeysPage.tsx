// API keys — the credential another platform integrates with.
//
// Two things this page has to get right, because both are one-way doors:
//
//   * The raw key is shown once. The reveal is a modal that says so and does
//     not close by accident, rather than a row in the table that silently
//     stops working on refresh.
//   * A key whose plan no longer grants its scopes is shown as *reduced*, not
//     as fine. The server already enforces that (`active_scopes`); hiding it
//     here would leave someone debugging a 403 with no way to see why.
import { useEffect, useState, FormEvent } from "react";
import { Link } from "react-router-dom";
import { KeyRound, Copy, Trash2, AlertTriangle, Lock } from "lucide-react";
import {
  ApiKey,
  ApiKeyList,
  CreatedApiKey,
  SCOPE_LABEL,
  createApiKey,
  getApiKeys,
  revokeApiKey,
} from "../api/apiKeys";
import { ApiError } from "../api/client";

function scopeLabel(scope: string): string {
  return SCOPE_LABEL[scope] ?? scope;
}

function CreateKeyModal({
  availableScopes,
  onCreated,
  onClose,
}: {
  availableScopes: string[];
  onCreated: (created: CreatedApiKey) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  // Everything the plan allows, pre-selected: that is what "create a key"
  // means to most people, and narrowing is the deliberate act.
  const [scopes, setScopes] = useState<string[]>(availableScopes);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CreatedApiKey | null>(null);
  const [copied, setCopied] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const created = await createApiKey(name, scopes);
      setResult(created);
      onCreated(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the key.");
    } finally {
      setLoading(false);
    }
  }

  function toggle(scope: string) {
    setScopes((prev) =>
      prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope],
    );
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="modal-overlay"
      // While the key is on screen, a stray click outside must not be what
      // destroys the only copy of it.
      onClick={(e) => !result && e.target === e.currentTarget && onClose()}
    >
      <div className="card shadow-modal w-full max-w-lg max-h-[90vh] overflow-y-auto animate-scale-in">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-900">
            {result ? "Copy your key now" : "Create an API key"}
          </h2>
          {!result && (
            <button onClick={onClose} aria-label="Close" className="btn-ghost p-1.5 h-auto">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        {result ? (
          <div className="px-6 py-5 space-y-4">
            <div className="rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 flex gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
              <p className="text-sm text-amber-800">
                This is the only time the key is shown. We store a hash of it, so
                it cannot be recovered — only replaced.
              </p>
            </div>
            <div>
              <label className="label">Secret key</label>
              <div className="flex items-center gap-2">
                <input readOnly value={result.raw_key} className="input flex-1 text-xs font-mono" />
                <button
                  type="button"
                  onClick={() => {
                    navigator.clipboard.writeText(result.raw_key);
                    setCopied(true);
                  }}
                  className="btn-secondary text-xs px-3 py-1.5 h-auto whitespace-nowrap"
                >
                  <Copy className="w-3.5 h-3.5" />
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
            <div>
              <label className="label">Send it as a header</label>
              <pre className="rounded-lg bg-gray-900 text-gray-100 text-xs p-3 overflow-x-auto">
{`curl https://your-domain/api/v1/chatbots \\
  -H "X-API-Key: ${result.raw_key}"`}
              </pre>
            </div>
            <div className="flex justify-end pt-2">
              <button type="button" onClick={onClose} className="btn-primary">
                I've saved it
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={submit}>
            <div className="px-6 py-5 space-y-4">
              <div>
                <label className="label">Name *</label>
                <input
                  required
                  className="input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Production integration"
                />
                <p className="text-xs text-gray-400 mt-1">
                  For your own reference — one key per integration makes a
                  compromised one safe to revoke on its own.
                </p>
              </div>
              <div>
                <label className="label">Permissions</label>
                <div className="space-y-2 mt-1">
                  {availableScopes.map((scope) => (
                    <label key={scope} className="flex items-center gap-2 text-sm text-gray-700">
                      <input
                        type="checkbox"
                        checked={scopes.includes(scope)}
                        onChange={() => toggle(scope)}
                      />
                      {scopeLabel(scope)}
                      <span className="text-xs text-gray-400 font-mono">{scope}</span>
                    </label>
                  ))}
                </div>
              </div>
              {error && (
                <div role="alert" className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
                  {error}
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-100 bg-gray-50/60">
              <button type="button" onClick={onClose} className="btn-secondary">
                Cancel
              </button>
              <button type="submit" disabled={loading || !name || scopes.length === 0} className="btn-primary">
                {loading ? "Creating…" : "Create key →"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** The paywall. Named plans and prices, because "upgrade to continue" with no
 * destination is the least useful thing a gate can say. */
function UpgradePrompt() {
  return (
    <div className="card p-8 text-center">
      <Lock className="w-8 h-8 text-gray-300 mx-auto" />
      <p className="text-sm font-semibold text-gray-900 mt-4">
        API keys are part of the paid plans
      </p>
      <p className="text-sm text-gray-500 mt-2 max-w-md mx-auto">
        Starter ($5/mo) and Growth ($10/mo) include the management API — create
        and configure assistants from your own platform. Scale ($15/mo) adds API
        consumption, where an assistant runs as an agent behind your key.
      </p>
      <Link to="/billing" className="btn-primary mt-5 inline-flex">
        See plans →
      </Link>
    </div>
  );
}

function KeyRow({ apiKey, onRevoke }: { apiKey: ApiKey; onRevoke: (id: string) => void }) {
  // The scopes the key was minted with but the plan no longer grants. Shown,
  // not swallowed — this is exactly what a surprise 403 looks like from here.
  const lost = apiKey.scopes.filter((s) => !apiKey.active_scopes.includes(s));

  return (
    <tr>
      <td className="font-medium text-gray-900">{apiKey.name}</td>
      <td className="font-mono text-xs text-gray-500">{apiKey.prefix}…</td>
      <td>
        <div className="flex flex-wrap gap-1">
          {apiKey.active_scopes.map((s) => (
            <span key={s} className="badge badge-live text-[11px]">
              {scopeLabel(s)}
            </span>
          ))}
          {lost.map((s) => (
            <span
              key={s}
              title="Your current plan no longer includes this permission"
              className="badge badge-paused text-[11px] line-through"
            >
              {scopeLabel(s)}
            </span>
          ))}
        </div>
      </td>
      <td className="text-gray-500 text-xs">
        {apiKey.last_used_at
          ? new Date(apiKey.last_used_at).toLocaleDateString([], {
              month: "short",
              day: "numeric",
            })
          : "Never used"}
      </td>
      <td>
        <button
          onClick={() => onRevoke(apiKey.id)}
          className="btn-ghost text-red-600 p-1.5 h-auto"
          aria-label={`Revoke ${apiKey.name}`}
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </td>
    </tr>
  );
}

export default function ApiKeysPage() {
  const [data, setData] = useState<ApiKeyList | null>(null);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getApiKeys()
      .then(setData)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load API keys."),
      )
      .finally(() => setLoading(false));
  }, []);

  async function revoke(id: string) {
    if (!confirm("Revoke this key? Anything using it stops working immediately.")) return;
    try {
      await revokeApiKey(id);
      setData((prev) =>
        prev ? { ...prev, keys: prev.keys.filter((k) => k.id !== id) } : prev,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not revoke the key.");
    }
  }

  const active = data?.keys.filter((k) => k.is_active) ?? [];

  return (
    <div className="page">
      {showCreate && data && (
        <CreateKeyModal
          availableScopes={data.available_scopes}
          onCreated={(created) =>
            setData((prev) =>
              prev ? { ...prev, keys: [created.key, ...prev.keys] } : prev,
            )
          }
          onClose={() => setShowCreate(false)}
        />
      )}

      <div className="page-header">
        <div>
          <h1 className="page-title">API Access</h1>
          <p className="text-sm text-gray-500 mt-1">
            Manage your API keys and integrate from your own platform.
          </p>
        </div>
        {data?.api_access && (
          <button onClick={() => setShowCreate(true)} className="btn-primary">
            <KeyRound className="w-4 h-4" />
            Add new key
          </button>
        )}
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
      ) : !data?.api_access ? (
        <UpgradePrompt />
      ) : (
        <>
          {!data.agent_api && (
            <div className="rounded-lg bg-teal-50 border border-teal-200 px-4 py-3 text-sm text-teal-900 mb-6">
              Your keys can manage assistants but not run them. API consumption —
              driving an assistant as an agent from your platform — is part of the
              Scale plan ($15/mo).{" "}
              <Link to="/billing" className="underline font-medium">
                Compare plans
              </Link>
            </div>
          )}

          <div className="card overflow-hidden">
            <div className="px-5 py-3 border-b border-gray-100">
              <p className="text-sm font-semibold text-gray-900">API keys</p>
              <p className="text-xs text-gray-500 mt-0.5">
                Send as an <span className="font-mono">X-API-Key</span> header. Keys
                carry only what your plan grants.
              </p>
            </div>
            {active.length === 0 ? (
              <p className="px-5 py-6 text-sm text-gray-500">
                No keys yet. Create one to start integrating.
              </p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Key</th>
                    <th>Permissions</th>
                    <th>Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {active.map((k) => (
                    <KeyRow key={k.id} apiKey={k} onRevoke={revoke} />
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </div>
  );
}
