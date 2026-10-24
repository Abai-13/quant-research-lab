import type { PracticeReport } from "./types";
import { formatDate, formatDateTime } from "./format";
import { ComparisonChart, PathMetrics } from "./ComparisonChart";
import { EmptyState, Notice, ScalarFields } from "./ui";

export function ReportView({
  report,
  revealedDate,
  hideSystem,
  loading,
  error,
  onRetry,
}: {
  report: PracticeReport | null;
  revealedDate: string | null;
  hideSystem: boolean;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <div className="empty-state">
        <p className="empty-title">正在生成复盘…</p>
      </div>
    );
  }
  if (error) {
    return (
      <Notice tone="error" title="复盘暂不可用">
        <p>{error}</p>
        <button type="button" className="refresh-button" onClick={onRetry}>
          重新读取
        </button>
      </Notice>
    );
  }
  if (!report) {
    return <EmptyState title="还没有复盘内容">结束模拟后会在这里生成报告。</EmptyState>;
  }

  return (
    <div className="practice-report">
      <p className="muted">
        报告只包含截至 {formatDate(revealedDate)} 的已揭晓区间；结束时不强制卖出，未知估值保持为空。
      </p>

      {Object.keys(report.summary).length > 0 ? (
        <div className="practice-card">
          <div className="practice-card-header">
            <h3>结果摘要</h3>
          </div>
          <ScalarFields record={report.summary} emptyText="报告没有返回摘要字段。" />
        </div>
      ) : null}

      <div className="practice-card">
        <div className="practice-card-header">
          <h3>四路径对照</h3>
        </div>
        <ComparisonChart comparison={report.comparison} revealedDate={revealedDate} hideSystem={hideSystem} />
        <PathMetrics comparison={report.comparison} hideSystem={hideSystem} />
        {report.comparison?.contributions && report.comparison.contributions.length > 0 ? (
          <div className="practice-contributions">
            <h4>算术贡献</h4>
            <ul>
              {report.comparison.contributions.map((item, index) => (
                <li key={index}>
                  {typeof item === "string" ? (
                    item
                  ) : typeof item === "object" && item !== null ? (
                    <ScalarFields record={item as Record<string, unknown>} emptyText="该项没有可展示的数值。" />
                  ) : (
                    String(item)
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {report.comparison?.limitations && report.comparison.limitations.length > 0 ? (
          <ul className="practice-restrictions">
            {report.comparison.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
      </div>

      <div className="practice-card">
        <div className="practice-card-header">
          <h3>决策记录</h3>
        </div>
        {report.decisions.length === 0 ? (
          <p className="muted">本次模拟没有决策记录。</p>
        ) : (
          <ol className="practice-decision-list">
            {report.decisions.map((decision) => (
              <li key={decision.decision_id}>
                <div className="practice-decision-meta">
                  <span>决策日 {formatDate(decision.decision_date)}</span>
                  <span>记录于 {formatDateTime(decision.recorded_at)}</span>
                  {decision.correction ? <span className="chip chip-warn">补记 / 更正</span> : null}
                </div>
                <p className="practice-decision-judgment">{decision.judgment}</p>
                {decision.evidence ? <p className="muted">依据：{decision.evidence}</p> : null}
                {decision.exit_condition ? (
                  <p className="muted">退出条件：{decision.exit_condition}</p>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </div>

      {report.limitations.length > 0 ? (
        <div className="practice-card">
          <div className="practice-card-header">
            <h3>报告限制</h3>
          </div>
          <ul className="practice-restrictions">
            {report.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="muted">
        复盘展示扣费后的结果、回撤、集中度、费用与交易次数；相关性与算术贡献不代表因果，也不保证系统建议胜出。
      </p>
    </div>
  );
}

