import { useEffect } from "react";
import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { hasToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { BlockedProvider, useBlocked } from "@/lib/blocked";
import { ProfileGateProvider, useProfileGate } from "@/lib/profile-gate";

function NavLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <Link
      to={to}
      className="whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-accent"
      activeProps={{ className: "bg-accent text-accent-foreground" }}
      activeOptions={{ exact: true }}
    >
      {children}
    </Link>
  );
}

function Nav() {
  const { blocked } = useBlocked();
  return (
    <nav className="flex flex-wrap items-center gap-1 border-b px-4 py-2" aria-label="Main">
      <span className="mr-4 font-semibold">CareerSurge</span>
      <NavLink to="/">Approve</NavLink>
      <NavLink to="/progress">Progress</NavLink>
      <NavLink to="/needs-you">Needs you{blocked.length > 0 ? ` (${blocked.length})` : ""}</NavLink>
      <NavLink to="/profile">Profile</NavLink>
      <NavLink to="/settings">Settings</NavLink>
    </nav>
  );
}

// Opens onboarding until it is done (a saved profile and a job title).
// Onboarding hides the nav: its steps are the only way forward.
function Gate() {
  const { onboarded, failed, refresh } = useProfileGate();
  const path = useRouterState({ select: (state) => state.location.pathname });
  const navigate = useNavigate();
  const onboarding = path === "/onboarding";
  useEffect(() => {
    if (onboarded === false && !onboarding) void navigate({ to: "/onboarding", replace: true });
  }, [onboarded, onboarding, navigate]);

  if (onboarded === null && failed !== null) {
    return (
      <main className="mx-auto max-w-xl p-8">
        <h1 className="mb-2 text-lg font-semibold">CareerSurge could not load</h1>
        <p className="mb-4 text-muted-foreground">{failed}</p>
        <Button onClick={() => void refresh()}>Try again</Button>
      </main>
    );
  }
  if (onboarded === null || (onboarded === false && !onboarding)) {
    return <p className="p-8 text-muted-foreground">Loading…</p>;
  }
  return (
    <BlockedProvider>
      {onboarded ? (
        <Nav />
      ) : (
        <nav className="border-b px-4 py-2" aria-label="Main">
          <span className="font-semibold">CareerSurge</span>
        </nav>
      )}
      <main className="mx-auto max-w-4xl p-4">
        <Outlet />
      </main>
    </BlockedProvider>
  );
}

export function Shell() {
  if (!hasToken()) {
    return (
      <main className="mx-auto max-w-xl p-8">
        <h1 className="mb-2 text-lg font-semibold">Open CareerSurge from its own window</h1>
        <p className="text-muted-foreground">
          This page needs the key the app gives its window at startup. Close this tab and start
          CareerSurge again.
        </p>
      </main>
    );
  }
  return (
    <ProfileGateProvider>
      <Gate />
    </ProfileGateProvider>
  );
}
