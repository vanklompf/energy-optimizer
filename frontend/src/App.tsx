import { useEffect, useState } from "react";
import { api, type AuthMeResponse } from "./api";
import { usePolling } from "./hooks";
import NowView from "./views/NowView";
import SavingsView from "./views/SavingsView";

type Tab = "now" | "savings";

const TABS: { id: Tab; label: string }[] = [
  { id: "now", label: "Dashboard" },
  { id: "savings", label: "Savings" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("now");
  const [auth, setAuth] = useState<AuthMeResponse | null>(null);
  const { data: status } = usePolling(api.status, 15000);
  const mode = status?.mode ?? "dry_run";
  const live = Boolean(status?.control_enabled);

  useEffect(() => {
    let active = true;
    api
      .authMe()
      .then((me) => {
        if (active) setAuth(me);
      })
      .catch(() => {
        // When OIDC is off the endpoint still returns 200; 401 redirects via api.ts.
      });
    return () => {
      active = false;
    };
  }, []);

  const showLogout = Boolean(auth?.oidc_enabled && auth.authenticated);
  const displayName =
    auth?.user?.name || auth?.user?.preferred_username || auth?.user?.email || null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" className="logo" width={24} height={24} />
          <span>Energy Optimizer</span>
          <span className={`badge ${live ? "badge-ok" : "badge-dryrun"}`}>{mode}</span>
        </div>
        <div className="topbar-right">
          <nav className="tabs">
            {TABS.map((t) => (
              <button
                key={t.id}
                className={t.id === tab ? "tab tab-active" : "tab"}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </nav>
          {showLogout && (
            <div className="auth-slot">
              {displayName && <span className="auth-user">{displayName}</span>}
              <a className="auth-logout" href="/auth/logout">
                Log out
              </a>
            </div>
          )}
        </div>
      </header>
      <main className="content">
        {tab === "now" && <NowView />}
        {tab === "savings" && <SavingsView />}
      </main>
    </div>
  );
}
