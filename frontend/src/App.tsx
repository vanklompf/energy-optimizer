import { useState } from "react";
import NowView from "./views/NowView";
import PlanView from "./views/PlanView";
import SavingsView from "./views/SavingsView";

type Tab = "now" | "plan" | "savings";

const TABS: { id: Tab; label: string }[] = [
  { id: "now", label: "Now" },
  { id: "plan", label: "Plan" },
  { id: "savings", label: "Savings" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("now");
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">⚡</span>
          <span>Energy Optimizer</span>
          <span className="badge badge-dryrun">dry_run</span>
        </div>
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
      </header>
      <main className="content">
        {tab === "now" && <NowView />}
        {tab === "plan" && <PlanView />}
        {tab === "savings" && <SavingsView />}
      </main>
    </div>
  );
}
