import { useEffect, useState } from "react";

import { DiagnosticsPage } from "./DiagnosticsPage";
import { ResearchSettings } from "./phase6/ResearchSettings";
import { CredentialsSettings } from "./phase6/CredentialsSettings";
import { DataPreparation } from "./phase6/DataPreparation";
import type { PreparedDataset } from "./phase6/types";
import { AgentPanel } from "./practice/AgentPanel";
import { PracticeWorkspace, type PracticeSection } from "./practice/PracticeWorkspace";
import "./practice.css";

type View = PracticeSection | "control";
type ControlTab = "model" | "data" | "storage";
import { StorageAndAutomation } from "./phase6/StorageAndAutomation";

function readView(): View {
  const route = window.location.hash.slice(1).split("/")[0];
  if (route === "control" || route === "research" || route === "settings" || route === "diagnostics") return "control";
  if (route === "room" || (route === "practice" && new URLSearchParams(window.location.search).has("practice"))) return "room";
  if (route === "records") return "records";
  return "home";
}

export default function App() {
  const [view, setView] = useState<View>(() => readView());
  const [controlTab, setControlTab] = useState<ControlTab>("model");
  const [guideOpen, setGuideOpen] = useState(() => {
    try { return window.localStorage.getItem("practice-guide-seen") !== "1"; } catch { return true; }
  });
  const [preparedDataset, setPreparedDataset] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    const onHashChange = () => setView(readView());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  function navigate(next: View) {
    const route = next === "control" ? "#control" : next === "room" ? "#room" : next === "records" ? "#records" : "#home";
    if (window.location.hash !== route) window.location.hash = route;
    else setView(next);
  }

  function closeGuide() {
    setGuideOpen(false);
    try { window.localStorage.setItem("practice-guide-seen", "1"); } catch { /* optional */ }
  }

  return (
    <div className="app-shell">
      <header className="app-nav">
        <div className="app-nav-inner">
          <div className="app-brand"><span className="app-brand-mark" aria-hidden="true" /><span>Agent 投资练习</span></div>
          <nav className="app-tabs" aria-label="页面导航">
            {([ ["home", "首页"], ["room", "练习室"], ["records", "练习记录"], ["control", "控制室"] ] as const).map(([key, label]) => (
              <button key={key} type="button" className={`app-tab ${view === key ? "app-tab-active" : ""}`} aria-current={view === key ? "page" : undefined} onClick={() => navigate(key)}>{label}</button>
            ))}
            <button type="button" className="app-tab" onClick={() => setGuideOpen(true)}>使用引导</button>
          </nav>
        </div>
      </header>

      {guideOpen ? <aside className="practice-guide-banner" role="status"><strong>下一步</strong><span>{view === "home" ? "准备本地合成示例，编辑方案并检查资格，然后由你确认创建。" : view === "room" ? "与 Agent 查证或手动下单；确认后再推进到显示的下一交易日。" : view === "records" ? "选择一段练习可继续查看进度或复盘报告。" : "模型和数据凭据分开管理；保存状态不代表已验证。"}</span><button type="button" className="link-button" onClick={closeGuide}>关闭</button></aside> : null}

      <main className="app-main">
        {view !== "control" ? <div className="page practice-page-shell"><PracticeWorkspace section={view} onNavigate={navigate} onGoControl={() => navigate("control")} setupDraft={preparedDataset} onSetupDraftConsumed={() => setPreparedDataset(null)} /></div> : (
          <div className="practice-page-shell control-room">
            <header className="practice-session-header"><div><p className="eyebrow">配置与系统状态</p><h1>控制室</h1><p className="practice-hero-lead">模型服务和历史行情数据来源分别管理。当前行情不会由最后一根历史 K 线推断。设置经现有安全接口保存；不会把密钥放进浏览器存储。</p></div></header>
            <div className="practice-mobile-tabs" role="group" aria-label="控制室分区"><button type="button" aria-pressed={controlTab === "model"} onClick={() => setControlTab("model")}>模型</button><button type="button" aria-pressed={controlTab === "data"} onClick={() => setControlTab("data")}>数据</button><button type="button" aria-pressed={controlTab === "storage"} onClick={() => setControlTab("storage")}>存储与策略</button></div>
            <section className={controlTab === "model" ? "control-room-panel" : "control-room-panel mobile-view-hidden"}><AgentPanel settingsOnly /></section>
            <section className={controlTab === "data" ? "control-room-panel" : "control-room-panel mobile-view-hidden"}><CredentialsSettings /><div className="practice-card"><h2>准备数据</h2><p className="muted">只提交本地后端明确支持的数据准备任务。准备完成后可带回练习方案，不会自动创建练习。</p><DataPreparation onUseDataset={(dataset: PreparedDataset) => { setPreparedDataset({ kind: "prepared_dataset", name: dataset.synthetic ? "合成示例练习" : "历史数据练习", start_date: dataset.start_date, end_date: dataset.end_date, data_version_id: dataset.data_version_id, source_code: dataset.source_code, symbols: dataset.symbols }); navigate("home"); }} /></div></section>
            <section className={controlTab === "storage" ? "control-room-panel" : "control-room-panel mobile-view-hidden"}><StorageAndAutomation initialPracticeId={new URLSearchParams(window.location.search).get("practice") ?? ""} /></section>
            <details className="practice-details"><summary>高级：运行与诊断</summary><DiagnosticsPage /></details>
            <details className="practice-details"><summary>高级：研究与实验</summary><ResearchSettings /></details>
          </div>
        )}
      </main>

      <footer className="app-footer"><span>Agent 投资练习 · 历史数据模拟</span><span>不接入真实资金；成交与复盘以所选数据和服务端规则为准</span></footer>
    </div>
  );
}
