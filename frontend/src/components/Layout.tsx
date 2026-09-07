// App shell: a sidebar that reveals itself in step with the workspace.
//
// The rail used to render all twenty-odd destinations from the first second of
// the first session, with Appointments as its first group. Someone who signed
// up to build a voice assistant landed on a menu whose top entry described a
// feature of an assistant they had not created yet, and no way to tell which
// row to touch first. The answer was "none of them" — most of that menu
// describes an assistant that does not exist.
//
// So each item now carries `unlockedAt`, and the rail shows what the workspace
// has actually earned. Three rules keep that from becoming its own problem:
//
//   * Voice AI Setup is always first. It is the product's point of entry and
//     the one group that is never gated.
//   * Nothing is ever *hidden*. Locked rows collapse into one disclosure at the
//     foot of the rail, so the shape of the product stays visible and "where
//     did X go" never happens.
//   * The stage is derived from the tenant's own data (see
//     store/onboarding.tsx), so an established workspace computes to `operate`
//     on first load and sees exactly what it saw yesterday.
//
// Groups run in the order someone actually moves through the product: build the
// thing, then let it take bookings, then watch it run, then run it at volume.
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  Bot, BookOpen, LineChart, Settings, LogOut, Users2, PhoneCall, Radio,
  Megaphone, Plug, Mic, LifeBuoy, Search, Bell, Moon, Sun,
  ListChecks, LayoutDashboard, PhoneOutgoing, MessageCircle, UserSearch,
  CalendarDays, CalendarCheck, Briefcase, MapPin, Clock,
  PanelLeftDashed, PanelLeftClose, PanelLeftOpen,
  ChevronDown, Lock, Eye, CreditCard, KeyRound,
} from "lucide-react";
import { useAuth } from "../store/auth";
import { useTheme } from "../store/theme";
import { NotificationsProvider, useNotifications } from "../store/notifications";
import { OnboardingProvider, useOnboarding } from "../store/onboarding";
import { atLeast, NavMode, Stage } from "../api/onboarding";
import { lockedRowLabel } from "../onboarding/stages";
import TourOverlay from "./onboarding/TourOverlay";
import NextStepCard from "./onboarding/NextStepCard";
import UnlockToast from "./onboarding/UnlockToast";

interface NavItem {
  to: string;
  label: string;
  icon: typeof Bot;
  exact?: boolean;
  /** Only shown to Owner/Admin roles — the "admin panel" surfaces. */
  adminOnly?: boolean;
  /** A fixed word, like "New" on a feature that just shipped. */
  badge?: string;
  /** A live count, resolved at render from `useNotifications`. Separate from
   * `badge` because the two behave differently: a word is decoration, a number
   * is something the user is expected to go and clear. */
  counter?: "newAppointments";
  /** The stage at which this row appears in the rail. Omitted = always. Below
   * it the row is not gone — it moves into the collapsed "more features" list,
   * and the page behind it stays reachable by URL and by ⌘K. */
  unlockedAt?: Stage;
}

interface NavGroup {
  title: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    // Its own group, above everything: the dashboard is where sign-in lands
    // and the one row that should never be hunted for inside a category.
    title: "Home",
    items: [
      { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard, exact: true },
    ],
  },
  {
    // First, permanently. This is what the product is for, and burying it
    // under the configuration of a feature of it — which is where it used to
    // sit — is how a new workspace ends up not knowing where to start.
    title: "Voice AI Setup",
    items: [
      { to: "/assistants", label: "Voice AI Assistants", icon: Bot },
      { to: "/clone-voice", label: "Clone Voice", icon: Mic },
      { to: "/knowledge", label: "Files", icon: BookOpen, unlockedAt: "teach" },
      {
        to: "/integrations",
        label: "Integrations",
        icon: Plug,
        adminOnly: true,
        unlockedAt: "teach",
      },
    ],
  },
  {
    // A daily workflow with its own configuration underneath, so it keeps its
    // own group rather than being a row inside Operations. Gated as a whole:
    // an assistant that isn't live has nothing to book.
    title: "Appointments",
    items: [
      { to: "/appointments/calendar", label: "Calendar", icon: CalendarDays, unlockedAt: "operate" },
      {
        to: "/appointments",
        label: "Appointments",
        icon: CalendarCheck,
        exact: true,
        counter: "newAppointments",
        unlockedAt: "operate",
      },
      {
        to: "/appointments/services",
        label: "Services",
        icon: Briefcase,
        adminOnly: true,
        unlockedAt: "operate",
      },
      {
        to: "/appointments/resources",
        label: "Staff & Resources",
        icon: Users2,
        adminOnly: true,
        unlockedAt: "operate",
      },
      {
        to: "/appointments/locations",
        label: "Locations",
        icon: MapPin,
        adminOnly: true,
        unlockedAt: "operate",
      },
      {
        to: "/appointments/availability",
        label: "Availability",
        icon: Clock,
        adminOnly: true,
        unlockedAt: "operate",
      },
    ],
  },
  {
    title: "Operations & Monitoring",
    items: [
      // Channels arrive at `launch` — that IS the launch step, so it has to be
      // in the rail at the moment the next-step card starts pointing at it.
      { to: "/channels", label: "Phone Numbers", icon: PhoneCall, adminOnly: true, unlockedAt: "launch" },
      {
        to: "/channels?tab=whatsapp",
        label: "WhatsApp Numbers",
        icon: MessageCircle,
        adminOnly: true,
        unlockedAt: "launch",
      },
      { to: "/candidates", label: "Candidates", icon: UserSearch, adminOnly: true, unlockedAt: "operate" },
      { to: "/interviews", label: "Call Logs", icon: PhoneOutgoing, adminOnly: true, unlockedAt: "operate" },
      { to: "/analytics", label: "Analytics", icon: LineChart, unlockedAt: "operate" },
    ],
  },
  {
    // Running it at volume — meaningless before it works at all.
    title: "Campaigns",
    items: [
      { to: "/interviews/bulk", label: "Bulk Call", icon: ListChecks, adminOnly: true, unlockedAt: "operate" },
      {
        to: "/broadcasts",
        label: "Broadcast",
        icon: Megaphone,
        adminOnly: true,
        badge: "New",
        unlockedAt: "operate",
      },
    ],
  },
  {
    // Never gated: account and support have to be reachable at every stage,
    // including — especially — by someone stuck on the first one.
    title: "Account & Billing",
    items: [
      { to: "/home", label: "Overview", icon: ListChecks, exact: true },
      { to: "/hiring-agent", label: "Hiring Agent", icon: Radio, adminOnly: true, unlockedAt: "operate" },
      // Never gated by stage: someone on the free tier who has hit the
      // one-assistant ceiling needs Billing on their first day, not after
      // they have earned their way to `operate`.
      { to: "/billing", label: "Billing", icon: CreditCard, adminOnly: true },
      { to: "/api-keys", label: "API", icon: KeyRound, adminOnly: true },
      { to: "/team", label: "Team", icon: Users2, adminOnly: true },
      { to: "/report-issue", label: "Report Issue", icon: LifeBuoy },
    ],
  },
];

/** Is this row in the rail yet? `full` is the permanent escape hatch behind
 * the rail's "Show all features" — once chosen it is never taken back. */
function isUnlocked(item: NavItem, stage: Stage, navMode: NavMode): boolean {
  return navMode === "full" || !item.unlockedAt || atLeast(stage, item.unlockedAt);
}

/** Everything the ⌘K palette can jump to, flattened out of the groups.
 *
 * Deliberately ignores `unlockedAt`: search is how someone who knows what they
 * want gets there, and a staged rail must never become a locked door. A page
 * reached this way renders normally — it just tends to be empty, which is its
 * own honest answer. */
function searchTargets(isAdmin: boolean): NavItem[] {
  return NAV_GROUPS.flatMap((g) => g.items).filter((i) => !i.adminOnly || isAdmin);
}

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const dark = theme === "dark";
  return (
    <button
      onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title={dark ? "Light theme" : "Dark theme"}
      className="chrome-control inline-flex items-center justify-center w-9 h-9 rounded-full
                 border chrome-rule"
    >
      {dark ? <Moon className="w-[18px] h-[18px]" strokeWidth={1.75} />
            : <Sun className="w-[18px] h-[18px]" strokeWidth={1.75} />}
    </button>
  );
}

/** Jump-to search. Filters the nav; Enter opens the top hit. */
function CommandSearch() {
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
      if (e.key === "Escape") {
        setOpen(false);
        inputRef.current?.blur();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const q = query.trim().toLowerCase();
  const hits = q
    ? searchTargets(isAdmin).filter((i) => i.label.toLowerCase().includes(q)).slice(0, 6)
    : [];

  function go(to: string) {
    navigate(to);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  }

  return (
    <div className="relative w-full max-w-md">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 pointer-events-none" strokeWidth={1.75} />
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        // Blur is delayed so a click on a result lands before the list unmounts.
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => { if (e.key === "Enter" && hits[0]) go(hits[0].to); }}
        placeholder="Search or jump to..."
        aria-label="Search or jump to a page"
        className="chrome-field w-full rounded-full pl-9 pr-14 py-2 text-[13px]"
      />
      <span className="absolute right-3 top-1/2 -translate-y-1/2 rounded border chrome-rule
                       px-1.5 py-0.5 text-[10px] font-medium text-gray-500 pointer-events-none">
        ⌘K
      </span>

      {open && hits.length > 0 && (
        <ul
          role="listbox"
          className="chrome-popover absolute z-50 mt-2 w-full overflow-hidden rounded-2xl
                     p-1.5 animate-scale-in"
        >
          {hits.map((hit) => {
            const Icon = hit.icon;
            return (
              <li key={hit.to}>
                <button
                  onMouseDown={() => go(hit.to)}
                  className="chrome-popover-item flex w-full items-center gap-2.5 rounded-xl
                             px-3 py-2 text-left text-[13px]"
                >
                  <Icon className="w-4 h-4 text-gray-500" strokeWidth={1.75} />
                  {hit.label}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * What the rail does when the pointer isn't on it.
 *
 *   auto   — collapses to icons while you work in the page, and comes back the
 *            moment you move left. The default: the nav is a place you pass
 *            through, and the 252px it costs is content width the whole time
 *            you are not using it.
 *   open   — always full width.
 *   closed — always icons.
 */
type RailMode = "auto" | "open" | "closed";

const RAIL_MODES: RailMode[] = ["auto", "open", "closed"];

const RAIL_LABEL: Record<RailMode, string> = {
  auto: "Auto-hide",
  open: "Keep open",
  closed: "Keep collapsed",
};

const RAIL_ICON: Record<RailMode, typeof Bot> = {
  auto: PanelLeftDashed,
  open: PanelLeftOpen,
  closed: PanelLeftClose,
};

/** Long enough that clipping the rail's corner on the way to a page control
 * doesn't yank it open, short enough that deliberately going for the nav feels
 * instant. */
const RAIL_HOVER_DELAY_MS = 90;

function readRailMode(): RailMode {
  const stored = localStorage.getItem("sidebarMode");
  if (stored === "auto" || stored === "open" || stored === "closed") return stored;
  // Migrates the previous boolean preference, so someone who had pinned the
  // rail shut does not get it swinging open on their next visit.
  return localStorage.getItem("sidebarCollapsed") === "1" ? "closed" : "auto";
}

/** The shell, wrapped so every page under it shares one notification poll
 * rather than each growing its own. */
export default function Layout() {
  return (
    <NotificationsProvider>
      {/* Mounted here rather than at the root: onboarding state is an
          authenticated read, and the root tree also renders the public
          landing page, the embed widget and the candidate interview screen. */}
      <OnboardingProvider>
        <AppShell />
      </OnboardingProvider>
    </NotificationsProvider>
  );
}

function AppShell() {
  const { logout, tenantId, isAdmin, email } = useAuth();
  const { newAppointments } = useNotifications();
  const { stage, navMode, setNavMode, loaded: stageKnown } = useOnboarding();
  const counters = { newAppointments };
  const navigate = useNavigate();
  const [mode, setMode] = useState<RailMode>(readRailMode);
  // The collapsed "N more features" list. Local, not persisted: peeking at
  // what's coming is a glance, not a preference — flipping "Show all features"
  // is how someone makes it permanent.
  const [lockedOpen, setLockedOpen] = useState(false);
  // Only meaningful in "auto". Held separately from `mode` so leaving the rail
  // never rewrites the stored preference.
  const [railHovered, setRailHovered] = useState(false);
  const hoverTimer = useRef<number | null>(null);

  useEffect(() => {
    localStorage.setItem("sidebarMode", mode);
  }, [mode]);

  // Clears a pending expand/collapse if the component goes away mid-gesture.
  useEffect(() => () => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
  }, []);

  function scheduleHover(next: boolean) {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setRailHovered(next), RAIL_HOVER_DELAY_MS);
  }

  /** Keyboard users get the labels too: tabbing into the rail expands it
   * immediately, with no hover to wait on. */
  function onRailFocus() {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    setRailHovered(true);
  }

  const collapsed = mode === "closed" || (mode === "auto" && !railHovered);

  // Until the first response lands (and there is no cached one to paint from),
  // the rail shows everything. Erring toward "too much" for a few hundred
  // milliseconds is recoverable; erring toward "too little" means an
  // established user watches their navigation appear out of nowhere, which
  // reads as the app having lost their workspace.
  const gate: NavMode = stageKnown ? navMode : "full";

  // What the current stage hasn't reached yet, flattened across every group and
  // in the groups' own order — so the disclosure reads as "the rest of the
  // product, in the order you'll meet it" rather than an arbitrary pile.
  const lockedItems = NAV_GROUPS.flatMap((g) => g.items).filter(
    (i) => (!i.adminOnly || isAdmin) && !isUnlocked(i, stage, gate),
  );

  function cycleMode() {
    setMode((m) => RAIL_MODES[(RAIL_MODES.indexOf(m) + 1) % RAIL_MODES.length]);
  }

  function handleLogout() {
    logout();
    navigate("/login");
  }

  const initials = (email || tenantId || "TA").slice(0, 2).toUpperCase();

  // Active items get a solid violet capsule with a glow — the same treatment
  // the reference gives its selected nav pill. Idle items stay quiet so the
  // active one is the only thing carrying colour in the rail.
  const navClass = ({ isActive }: { isActive: boolean }) =>
    `group relative flex items-center gap-3 rounded-full px-3 py-2 text-[13px] font-semibold
     transition-all duration-200 ${
       isActive
         ? "bg-gradient-to-r from-brand-500 to-brand-500/70 text-white shadow-[0_4px_16px_-4px_rgba(139,92,246,0.65)]"
         : "chrome-item"
     }`;

  return (
    // No opaque background here: the aurora painted on <body> shows through the
    // whole shell, and the chrome floats on it as frosted glass.
    <div className="flex h-screen overflow-hidden">
      {/* ── Sidebar ── */}
      <nav
        aria-label="Primary navigation"
        // Hovering the rail opens it; hovering anything else lets it close
        // again. Wired here rather than on the main column so the pointer
        // crossing the gap between them is one event, not two racing ones.
        onMouseEnter={() => scheduleHover(true)}
        onMouseLeave={() => scheduleHover(false)}
        onFocusCapture={onRailFocus}
        onBlurCapture={(e) => {
          // Only when focus actually left the rail — moving between two nav
          // links fires blur as well, and collapsing there would hide the
          // labels mid-tab.
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
            setRailHovered(false);
          }
        }}
        className={`relative z-20 flex-shrink-0 glass-chrome flex flex-col select-none border-r
                    transition-[width] duration-300 ${collapsed ? "w-[68px]" : "w-[252px]"}`}
      >
        {/* Violet bloom behind the logo, and a coral one at the foot — the rail
            reads as lit from within rather than as a flat column. `chrome-bloom`
            dims both in the light theme, where the same alphas stain rather
            than glow. */}
        <div className="chrome-bloom pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,rgba(139,92,246,0.18),transparent_55%)]" />
        <div className="chrome-bloom pointer-events-none absolute bottom-0 inset-x-0 h-64 bg-[radial-gradient(circle_at_50%_100%,rgba(249,115,60,0.10),transparent_60%)]" />

        {/* Logo */}
        <div className="relative flex items-center gap-2.5 px-4 h-[64px] flex-shrink-0">
          <div className="relative w-9 h-9 rounded-xl bg-gradient-to-br from-cta-400 via-brand-500 to-brand-700
                          flex items-center justify-center flex-shrink-0
                          shadow-[0_6px_20px_-6px_rgba(139,92,246,0.8)]">
            <Bot className="w-[19px] h-[19px] text-white" strokeWidth={2} />
          </div>
          {!collapsed && (
            <span className="chrome-brand font-display font-bold text-[16px] tracking-tight truncate">
              Evara<span className="text-aurora">AI</span>
            </span>
          )}
        </div>

        {/* The one thing to do next. Above the groups on purpose: it is the
            instruction for how to read them. */}
        <div className="relative px-3">
          <NextStepCard collapsed={collapsed} />
        </div>

        {/* Groups */}
        <div className="relative flex-1 px-3 pb-2 overflow-y-auto">
          {NAV_GROUPS.map((group) => {
            const visible = group.items.filter((i) => !i.adminOnly || isAdmin);
            // A group whose every row is still locked disappears entirely —
            // an empty heading is worse than no heading. Its rows are not
            // lost; they are in the disclosure below.
            const items = visible.filter((i) => isUnlocked(i, stage, gate));
            if (items.length === 0) return null;
            return (
              <div key={group.title} className="mb-4">
                {!collapsed && (
                  // gray-500, not gray-600: the grey scale inverts with the
                  // theme, and gray-600 is a *dark* grey in light — which is
                  // how these headings ended up invisible.
                  <p className="px-3 pb-1.5 pt-2 text-[10.5px] font-semibold uppercase tracking-[0.09em] text-gray-500">
                    {group.title}
                  </p>
                )}
                {collapsed && <div className="mx-3 my-3 border-t chrome-rule" />}
                <ul className="space-y-0.5" role="list">
                  {items.map((item) => {
                    const Icon = item.icon;
                    const count = item.counter ? counters[item.counter] : 0;
                    return (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          end={item.exact}
                          title={collapsed ? item.label : undefined}
                          className={navClass}
                          data-tour={item.to === "/assistants" ? "nav-assistants" : undefined}
                        >
                          {({ isActive }) => (
                            <>
                              <Icon
                                className={`w-[18px] h-[18px] flex-shrink-0 transition-colors ${
                                  isActive ? "text-white" : "text-gray-500 group-hover:text-gray-800"
                                }`}
                                strokeWidth={1.75}
                              />
                              {/* Collapsed, the rail is icons only — but a
                                  count is exactly the thing you must not have
                                  to expand the rail to notice, so it becomes a
                                  dot on the icon's corner instead. */}
                              {collapsed && count > 0 && (
                                <span
                                  aria-hidden="true"
                                  className="absolute right-2 top-1.5 h-2 w-2 rounded-full
                                             bg-cta-400 ring-2 ring-chrome"
                                />
                              )}
                              {!collapsed && (
                                <>
                                  <span className="truncate">{item.label}</span>
                                  {count > 0 && (
                                    <span
                                      title={`${count} new since you last looked`}
                                      className={`ml-auto flex h-[18px] min-w-[18px] items-center
                                                  justify-center rounded-full px-1 text-[10px]
                                                  font-bold tabular-nums ${
                                                    isActive
                                                      ? "bg-white/25 text-white"
                                                      : "bg-cta-500 text-white"
                                                  }`}
                                    >
                                      {count > 99 ? "99+" : count}
                                    </span>
                                  )}
                                  {item.badge && count === 0 && (
                                    <span className={`ml-auto rounded-full px-1.5 py-0.5 text-[9.5px] font-bold
                                                     uppercase tracking-wide ${
                                                       isActive
                                                         ? "bg-white/20 text-white"
                                                         : "bg-cta-500/20 text-cta-400"
                                                     }`}>
                                      {item.badge}
                                    </span>
                                  )}
                                </>
                              )}
                            </>
                          )}
                        </NavLink>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}

          {/* Everything not unlocked yet — one row, not twenty.
              The whole reason this is a disclosure rather than a deletion: a
              staged rail that simply removed rows would leave people asking
              where a feature went, and would hide the fact that the product
              does more than the four things currently on screen. */}
          {!collapsed && lockedItems.length > 0 && (
            <div className="mb-4">
              <button
                type="button"
                onClick={() => setLockedOpen((o) => !o)}
                aria-expanded={lockedOpen}
                className="chrome-item flex w-full items-center gap-3 rounded-full px-3 py-2
                           text-[13px] font-semibold"
              >
                <Lock className="w-[18px] h-[18px] flex-shrink-0 text-gray-500" strokeWidth={1.75} />
                <span className="truncate">{lockedRowLabel(lockedItems.length)}</span>
                <ChevronDown
                  className={`ml-auto w-4 h-4 flex-shrink-0 text-gray-500 transition-transform
                              ${lockedOpen ? "rotate-180" : ""}`}
                  strokeWidth={2}
                />
              </button>

              {lockedOpen && (
                <>
                  <ul className="mt-1 space-y-0.5" role="list">
                    {lockedItems.map((item) => {
                      const Icon = item.icon;
                      return (
                        <li key={item.to}>
                          {/* A real link, not a disabled row. Locked means
                              "not on your path yet", never "you may not". */}
                          <NavLink
                            to={item.to}
                            end={item.exact}
                            className="chrome-item flex items-center gap-3 rounded-full px-3 py-2
                                       text-[13px] font-semibold opacity-45 hover:opacity-100"
                          >
                            <Icon className="w-[18px] h-[18px] flex-shrink-0 text-gray-500" strokeWidth={1.75} />
                            <span className="truncate">{item.label}</span>
                          </NavLink>
                        </li>
                      );
                    })}
                  </ul>
                  <button
                    type="button"
                    onClick={() => setNavMode("full")}
                    className="mt-1.5 w-full rounded-full px-3 py-1.5 text-left text-[11.5px]
                               font-semibold text-brand-400 transition-colors hover:text-brand-300"
                  >
                    Show all features permanently
                  </button>
                </>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="relative px-3 py-3 space-y-0.5 border-t chrome-rule">
          <button
            onClick={cycleMode}
            aria-expanded={!collapsed}
            title={`Sidebar: ${RAIL_LABEL[mode]} — click to change`}
            className="chrome-item flex w-full items-center gap-3 rounded-full px-3 py-2
                       text-[13px] font-semibold"
          >
            {(() => {
              const ModeIcon = RAIL_ICON[mode];
              return <ModeIcon className="w-[18px] h-[18px] flex-shrink-0" strokeWidth={1.75} />;
            })()}
            {!collapsed && RAIL_LABEL[mode]}
          </button>

          {navMode === "full" && (
            // Only offered once "Show all features" has been used. The way
            // back matters as much as the way out: someone who opened the full
            // menu to find one page shouldn't be stuck with twenty forever.
            <button
              onClick={() => setNavMode("guided")}
              title={collapsed ? "Show only what I've set up" : undefined}
              className="chrome-item flex w-full items-center gap-3 rounded-full px-3 py-2
                         text-[13px] font-semibold"
            >
              <Eye className="w-[18px] h-[18px] flex-shrink-0" strokeWidth={1.75} />
              {!collapsed && "Simplify my menu"}
            </button>
          )}

          {isAdmin && (
            <NavLink to="/settings" title={collapsed ? "Settings" : undefined} className={navClass}>
              {({ isActive }) => (
                <>
                  <Settings
                    className={`w-[18px] h-[18px] flex-shrink-0 ${isActive ? "text-white" : "text-gray-500"}`}
                    strokeWidth={1.75}
                  />
                  {!collapsed && "Settings"}
                </>
              )}
            </NavLink>
          )}

          <button
            onClick={handleLogout}
            title={collapsed ? "Logout" : undefined}
            className="flex w-full items-center gap-3 rounded-full px-3 py-2 text-[13px] font-semibold
                       text-gray-400 transition-colors hover:bg-red-500/10 hover:text-red-400"
          >
            <LogOut className="w-[18px] h-[18px] flex-shrink-0" strokeWidth={1.75} />
            {!collapsed && "Logout"}
          </button>
        </div>
      </nav>

      {/* ── Main column ── */}
      {/* The other half of the auto-hide gesture: putting the pointer anywhere
          in the page lets the rail fall back to icons. */}
      <div
        className="flex-1 flex flex-col min-w-0"
        onMouseEnter={() => scheduleHover(false)}
      >
        {/* Top bar */}
        <header className="relative flex-shrink-0 h-[64px] glass-chrome border-b
                           flex items-center gap-4 px-6">
          <div className="flex-1 flex justify-center">
            <CommandSearch />
          </div>

          <div className="flex items-center gap-2.5 flex-shrink-0">
            <button
              aria-label="Notifications"
              className="chrome-control relative inline-flex items-center justify-center
                         w-9 h-9 rounded-full"
            >
              <Bell className="w-[18px] h-[18px]" strokeWidth={1.75} />
            </button>
            <div
              title={email || undefined}
              className="inline-flex items-center justify-center w-9 h-9 rounded-full
                         bg-gradient-to-br from-cta-400 via-brand-500 to-brand-700 text-white text-[11px]
                         font-bold uppercase tracking-tight
                         shadow-[0_4px_14px_-4px_rgba(139,92,246,0.7)]"
            >
              {initials}
            </div>
            <ThemeToggle />
          </div>
        </header>

        {/* Transparent so the body aurora reads through the content column. */}
        <main className="flex-1 overflow-y-auto min-h-0">
          <Outlet />
        </main>
      </div>

      <TourOverlay />
      <UnlockToast />
    </div>
  );
}
