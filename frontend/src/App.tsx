import { useEffect, useState } from "react";

import { DiagnosticsPage } from "./DiagnosticsPage";
import { PracticeWorkspace } from "./practice/PracticeWorkspace";
import "./practice.css";

type View = "practice" | "diagnostics";

function readView(): View {
  return window.location.hash === "#diagnostics" ? "diagnostics" : "practice";
}

export default function App() {
  const [view, setView] = useState<View>(() => readView());

  useEffect(() => {
    const onHashChange = () => setView(readView());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  function switchView(next: View) {
    if (next === view) {
      return;
    }
    const url = new URL(window.location.href);
    if (next === "diagnostics") {
      url.hash = "diagnostics";
    } else {
      url.hash = "";
    }
    window.history.replaceState({}, "", url);
    setView(next);
  }

  return (
    <div className="app-shell">
      <header className="app-nav">
        <div className="app-nav-inner">
          <div className="app-brand">
            <span className="app-brand-mark" aria-hidden="true" />
            <span>AI 量化模拟交易系统</span>
          </div>
          <nav className="app-tabs" aria-label="页面导航">
            <button
              type="button"
              className={`app-tab ${view === "practice" ? "app-tab-active" : ""}`}
              aria-current={view === "practice" ? "page" : undefined}
              onClick={() => switchView("practice")}
            >
              历史模拟
            </button>
            <button
              type="button"
              className={`app-tab ${view === "diagnostics" ? "app-tab-active" : ""}`}
              aria-current={view === "diagnostics" ? "page" : undefined}
              onClick={() => switchView("diagnostics")}
            >
              运行与诊断
            </button>
          </nav>
        </div>
      </header>

      <main className="app-main">
        {view === "practice" ? (
          <div className="page practice-page-shell">
            <PracticeWorkspace />
          </div>
        ) : (
          <DiagnosticsPage />
        )}
      </main>

      <footer className="app-footer">
        <span>历史模拟 · Agent 任务工作台</span>
        <span>仅作历史规则下的模拟，不接入真实资金</span>
        <span>工具步骤与依据可逐项核对</span>
      </footer>
    </div>
  );
}
