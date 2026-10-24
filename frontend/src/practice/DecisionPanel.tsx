import { useRef, useState } from "react";

import type { PracticeDecision } from "./types";
import { formatDate, formatDateTime, toApiDate } from "./format";
import { EmptyState, Notice } from "./ui";

export function DecisionPanel({
  decisions,
  finished,
  busy,
  onAdd,
}: {
  decisions: PracticeDecision[];
  finished: boolean;
  busy: boolean;
  onAdd: (input: {
    judgment: string;
    evidence: string;
    exit_condition: string;
    decision_date?: string;
    key: string;
  }) => Promise<void>;
}) {
  const [judgment, setJudgment] = useState("");
  const [evidence, setEvidence] = useState("");
  const [exitCondition, setExitCondition] = useState("");
  const [decisionDate, setDecisionDate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const keyRef = useRef<{ key: string; signature: string } | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    if (judgment.trim() === "") {
      setError("请写下你的判断原文；证据与退出条件可以留空。");
      return;
    }
    const apiDate = decisionDate ? toApiDate(decisionDate) : null;
    if (decisionDate && !apiDate) {
      setError("补记日期无效，请重新选择。");
      return;
    }
    const signature = JSON.stringify({
      judgment: judgment.trim(),
      evidence: evidence.trim(),
      exitCondition: exitCondition.trim(),
      apiDate,
    });
    if (keyRef.current?.signature !== signature) {
      keyRef.current = { key: crypto.randomUUID(), signature };
    }
    setSaving(true);
    try {
      await onAdd({
        judgment: judgment.trim(),
        evidence: evidence.trim(),
        exit_condition: exitCondition.trim(),
        decision_date: apiDate ?? undefined,
        key: keyRef.current.key,
      });
      keyRef.current = null;
      setJudgment("");
      setEvidence("");
      setExitCondition("");
      setDecisionDate("");
      setStatus("决策记录已追加保存；修改会以新的更正记录出现，不会覆盖原文。");
    } catch (submitError) {
      setError(
        submitError instanceof Error ? submitError.message : "保存决策记录失败，请稍后重试。",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="practice-card">
      <div className="practice-card-header">
        <div>
          <h3>决策记录</h3>
          <p className="muted">
            可选。记录判断原文、依据与退出条件，便于结束后复盘；不会触发任何交易。
          </p>
        </div>
      </div>

      {decisions.length === 0 ? (
        <EmptyState title="还没有决策记录">
          可以跳过记录，直接下单或揭晓下一交易日；之后也可以补记。
        </EmptyState>
      ) : (
        <ol className="practice-decision-list">
          {decisions.map((decision) => (
            <li key={decision.decision_id}>
              <div className="practice-decision-meta">
                <span>决策日 {formatDate(decision.decision_date)}</span>
                <span>记录于 {formatDateTime(decision.recorded_at)}</span>
                {decision.correction ? <span className="chip chip-warn">补记 / 更正</span> : null}
              </div>
              <p className="practice-decision-judgment">{decision.judgment}</p>
              {decision.evidence ? (
                <p className="muted">依据：{decision.evidence}</p>
              ) : null}
              {decision.exit_condition ? (
                <p className="muted">退出条件：{decision.exit_condition}</p>
              ) : null}
            </li>
          ))}
        </ol>
      )}

      {finished ? (
        <Notice tone="neutral">练习已结束，决策记录保持只读。</Notice>
      ) : (
        <form className="practice-form" onSubmit={(event) => void handleSubmit(event)}>
          <label className="practice-field">
            <span>判断原文（必填）</span>
            <textarea
              rows={3}
              value={judgment}
              disabled={busy || saving}
              onChange={(event) => setJudgment(event.target.value)}
            />
          </label>
          <div className="practice-form-grid">
            <label className="practice-field">
              <span>依据（可空）</span>
              <textarea
                rows={2}
                value={evidence}
                disabled={busy || saving}
                onChange={(event) => setEvidence(event.target.value)}
              />
            </label>
            <label className="practice-field">
              <span>退出条件（可空）</span>
              <textarea
                rows={2}
                value={exitCondition}
                disabled={busy || saving}
                onChange={(event) => setExitCondition(event.target.value)}
              />
            </label>
            <label className="practice-field">
              <span>补记日期（可选）</span>
              <input
                type="date"
                value={decisionDate}
                disabled={busy || saving}
                onChange={(event) => setDecisionDate(event.target.value)}
              />
            </label>
          </div>
          {error ? <Notice tone="error">{error}</Notice> : null}
          {status ? <Notice tone="ok">{status}</Notice> : null}
          <div className="practice-form-actions">
            <button type="submit" className="refresh-button" disabled={busy || saving}>
              {saving ? "保存中…" : "追加决策记录"}
            </button>
            <span className="muted">补记日期留空时按当前已揭晓日记录。</span>
          </div>
        </form>
      )}
    </div>
  );
}

