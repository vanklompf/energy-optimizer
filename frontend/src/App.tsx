import { useState } from "react";
import NowView from "./views/NowView";
import SavingsView from "./views/SavingsView";

type Tab = "now" | "savings";

const TABS: { id: Tab; label: string }[] = [
  { id: "now", label: "Dashboard" },
  { id: "savings", label: "Savings" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("now");
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" className="logo" width={24} height={24} />
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
        {tab === "savings" && <SavingsView />}
      </main>
    </div>
  );
}
