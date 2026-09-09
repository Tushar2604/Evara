import {
  createContext,
  useContext,
  useState,
  useCallback,
  ReactNode,
} from "react";
import { login as apiLogin, register as apiRegister, TokenResponse } from "../api/auth";

const ADMIN_ROLES = ["owner", "admin"];

interface AuthState {
  accessToken: string | null;
  tenantId: string | null;
  userId: string | null;
  role: string | null;
  /** Captured at sign-in for the avatar tooltip — the token response does
   * not carry it, and null is fine (the UI falls back to the tenant id). */
  email: string | null;
  /** Platform-wide, independent of `role` (which is tenant-scoped) — gates
   * the Super Admin panel. */
  isPlatformAdmin: boolean;
}

interface AuthContextValue extends AuthState {
  isAuthenticated: boolean;
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (tenantName: string, email: string, password: string) => Promise<void>;
  /** Same effect as login/register — used after accepting a team invite,
   * which returns the same TokenResponse shape. */
  applySession: (resp: TokenResponse) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function saveTokens(resp: TokenResponse, email?: string) {
  if (email) localStorage.setItem("email", email);
  localStorage.setItem("access_token", resp.access_token);
  localStorage.setItem("refresh_token", resp.refresh_token);
  localStorage.setItem("tenant_id", resp.tenant_id);
  localStorage.setItem("user_id", resp.user_id);
  localStorage.setItem("role", resp.role);
  localStorage.setItem("is_platform_admin", resp.is_platform_admin ? "1" : "0");
}

function clearTokens() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  localStorage.removeItem("tenant_id");
  localStorage.removeItem("user_id");
  localStorage.removeItem("role");
  localStorage.removeItem("email");
  localStorage.removeItem("is_platform_admin");
}

function readState(): AuthState {
  return {
    accessToken: localStorage.getItem("access_token"),
    tenantId: localStorage.getItem("tenant_id"),
    userId: localStorage.getItem("user_id"),
    role: localStorage.getItem("role"),
    email: localStorage.getItem("email"),
    isPlatformAdmin: localStorage.getItem("is_platform_admin") === "1",
  };
}

function stateFromResponse(resp: TokenResponse, email?: string): AuthState {
  return {
    accessToken: resp.access_token,
    tenantId: resp.tenant_id,
    userId: resp.user_id,
    role: resp.role,
    email: email ?? localStorage.getItem("email"),
    isPlatformAdmin: !!resp.is_platform_admin,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(readState);

  const login = useCallback(async (email: string, password: string) => {
    const resp = await apiLogin(email, password);
    saveTokens(resp, email);
    setState(stateFromResponse(resp, email));
  }, []);

  const register = useCallback(async (tenantName: string, email: string, password: string) => {
    const resp = await apiRegister(tenantName, email, password);
    saveTokens(resp, email);
    setState(stateFromResponse(resp, email));
  }, []);

  const applySession = useCallback((resp: TokenResponse) => {
    saveTokens(resp);
    setState(stateFromResponse(resp));
  }, []);

  const logout = useCallback(() => {
    clearTokens();
    setState({
      accessToken: null,
      tenantId: null,
      userId: null,
      role: null,
      email: null,
      isPlatformAdmin: false,
    });
  }, []);

  return (
    <AuthContext.Provider
      value={{
        ...state,
        isAuthenticated: !!state.accessToken,
        isAdmin: !!state.role && ADMIN_ROLES.includes(state.role),
        login,
        register,
        applySession,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
