import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./store/auth";
import ProtectedRoute from "./components/ProtectedRoute";
import Layout from "./components/Layout";

import LandingPage        from "./pages/LandingPage";
import { LocaleProvider } from "./store/locale";
import LoginPage          from "./pages/LoginPage";
import RegisterPage       from "./pages/RegisterPage";
import WidgetChatPage     from "./pages/WidgetChatPage";
import EmbedChatPage      from "./pages/EmbedChatPage";
import InterviewCallPage  from "./pages/InterviewCallPage";
import DashboardPage      from "./pages/DashboardPage";
import HomePage           from "./pages/HomePage";
import AssistantsPage     from "./pages/AssistantsPage";
import AssistantDetailPage from "./pages/AssistantDetailPage";
import KnowledgePage      from "./pages/KnowledgePage";
import InterviewsPage     from "./pages/InterviewsPage";
import BulkInterviewPage  from "./pages/BulkInterviewPage";
import InterviewDetailPage from "./pages/InterviewDetailPage";
import ChannelsPage       from "./pages/ChannelsPage";
import BroadcastsPage     from "./pages/BroadcastsPage";
import BroadcastCreatePage from "./pages/BroadcastCreatePage";
import WhatsAppInboxPage from "./pages/WhatsAppInboxPage";
import CandidatesPage     from "./pages/CandidatesPage";
import CandidateProfilePage from "./pages/CandidateProfilePage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import BroadcastDetailPage from "./pages/BroadcastDetailPage";
import IntegrationsPage   from "./pages/IntegrationsPage";
import CloneVoicePage     from "./pages/CloneVoicePage";
import ReportIssuePage    from "./pages/ReportIssuePage";
import AnalyticsPage      from "./pages/AnalyticsPage";
import SettingsPage       from "./pages/SettingsPage";
import HiringAgentPage    from "./pages/HiringAgentPage";
import TeamPage           from "./pages/TeamPage";
import BillingPage        from "./pages/BillingPage";
import ApiKeysPage        from "./pages/ApiKeysPage";
import AcceptInvitePage   from "./pages/AcceptInvitePage";
import AppointmentsCalendarPage from "./pages/AppointmentsCalendarPage";
import AppointmentsPage  from "./pages/AppointmentsPage";
import ServicesPage      from "./pages/ServicesPage";
import ResourcesPage     from "./pages/ResourcesPage";
import LocationsPage     from "./pages/LocationsPage";
import AvailabilityPage  from "./pages/AvailabilityPage";
import PlatformAdminPage from "./pages/PlatformAdminPage";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          {/* Public marketing landing — the front door. Everything behind
              /home is unchanged; this only occupies the previously-redirecting
              "/" slot. */}
          <Route
            path="/"
            // The provider wraps only this route. The console is a
            // signed-in workspace in one language; the landing page is a
            // public page a stranger arrives at in theirs, and tying them
            // together would hand a Spanish visitor who signs up a
            // half-translated dashboard.
            element={
              <LocaleProvider>
                <LandingPage />
              </LocaleProvider>
            }
          />

          {/* Public routes */}
          <Route path="/login"    element={<LoginPage />}    />
          <Route path="/register" element={<RegisterPage />} />
          {/* Public: reached from an emailed link, so no session exists yet. */}
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password"  element={<ResetPasswordPage />} />

          {/* Public widget — unauthenticated share link */}
          <Route path="/c/:publicKey"     element={<WidgetChatPage />} />
          {/* Iframe-optimized embed — no platform chrome, postMessage API */}
          <Route path="/embed/:publicKey" element={<EmbedChatPage />} />
          {/* Candidate-facing virtual interview — token-scoped, no account needed */}
          <Route path="/interview/:token" element={<InterviewCallPage />} />
          {/* Accept a team invite — token-scoped, no account needed yet */}
          <Route path="/accept-invite/:token" element={<AcceptInvitePage />} />

          {/* Authenticated routes */}
          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              {/* The landing screen after sign-in. `/home` stays the
                  operational Overview it has always been. */}
              <Route path="/dashboard"  element={<DashboardPage />}      />
              <Route path="/home"       element={<HomePage />}           />
              <Route path="/assistants" element={<AssistantsPage />}     />
              <Route path="/assistants/:id" element={<AssistantDetailPage />} />
              <Route path="/knowledge"  element={<KnowledgePage />}      />
              <Route path="/analytics"  element={<AnalyticsPage />}      />
              <Route path="/clone-voice" element={<CloneVoicePage />}   />
              <Route path="/report-issue" element={<ReportIssuePage />} />

              {/* Appointments. The calendar and the list are open to any
                  signed-in user (receptionists need them); the
                  configuration screens are admin-only, below. */}
              <Route path="/appointments/calendar" element={<AppointmentsCalendarPage />} />
              <Route path="/appointments" element={<AppointmentsPage />} />

              {/* Admin panel: Owner/Admin roles only */}
              <Route element={<ProtectedRoute requireAdmin />}>
                <Route path="/interviews" element={<InterviewsPage />}     />
                <Route path="/interviews/bulk" element={<BulkInterviewPage />} />
                <Route path="/interviews/:id" element={<InterviewDetailPage />} />
                <Route path="/channels"  element={<ChannelsPage />}      />
                {/* The chat window for one QR-linked number. */}
                <Route path="/channels/whatsapp/:sessionId/inbox" element={<WhatsAppInboxPage />} />
                {/* Every WhatsApp contact across every number, read-oriented. */}
                <Route path="/candidates" element={<CandidatesPage />} />
                <Route path="/candidates/:candidateId" element={<CandidateProfilePage />} />
                <Route path="/integrations"   element={<IntegrationsPage />}    />
                <Route path="/broadcasts"     element={<BroadcastsPage />}      />
                {/* Registered before /:id so "new" is a page, not an id. */}
                <Route path="/broadcasts/new" element={<BroadcastCreatePage />} />
                <Route path="/broadcasts/:id" element={<BroadcastDetailPage />} />
                <Route path="/hiring-agent" element={<HiringAgentPage />}  />
                <Route path="/team"       element={<TeamPage />}           />
                {/* Account-level, so admin-gated like /team: a key is full
                    tenant scope and a plan change is a charge. */}
                <Route path="/billing"    element={<BillingPage />}        />
                <Route path="/api-keys"   element={<ApiKeysPage />}        />
                <Route path="/settings"   element={<SettingsPage />}       />
                <Route path="/appointments/services"     element={<ServicesPage />} />
                <Route path="/appointments/resources"    element={<ResourcesPage />} />
                <Route path="/appointments/locations"    element={<LocationsPage />} />
                <Route path="/appointments/availability" element={<AvailabilityPage />} />
              </Route>

              {/* Super Admin: platform-wide, independent of the tenant-scoped
                  admin block above — gated on `isPlatformAdmin`, not `role`. */}
              <Route element={<ProtectedRoute requirePlatformAdmin />}>
                <Route path="/platform-admin" element={<PlatformAdminPage />} />
              </Route>

              {/* Legacy redirects so old bookmarks keep working */}
              <Route path="/documents"                         element={<Navigate to="/knowledge"  replace />} />
              <Route path="/chatbots"                          element={<Navigate to="/assistants" replace />} />
              <Route path="/chatbots/:id"                      element={<Navigate to="/assistants" replace />} />
              <Route path="/chatbots/:id/settings"             element={<Navigate to="/assistants" replace />} />
            </Route>
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
