import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "../store/auth";

export default function ProtectedRoute({
  requireAdmin = false,
  requirePlatformAdmin = false,
}: {
  requireAdmin?: boolean;
  requirePlatformAdmin?: boolean;
}) {
  const { isAuthenticated, isAdmin, isPlatformAdmin } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (requireAdmin && !isAdmin) return <Navigate to="/dashboard" replace />;
  if (requirePlatformAdmin && !isPlatformAdmin) return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
