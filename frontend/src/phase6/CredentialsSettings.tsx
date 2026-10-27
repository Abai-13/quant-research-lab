import { useCallback, useEffect, useState } from "react";
import type { CredentialPublic } from "./types";
import { clearCredential, listCredentials, newIdempotencyKey, saveCredential } from "./client";

function message(error: unknown) {
  return error instanceof Error ? error.message : "凭据请求失败，请重试。";
}

function stateLabel(item: CredentialPublic) {
  if (item.state === "configured") return "已保存，供应商尚未检测";
  if (item.state === "cleared") return "已清除";
  if (item.state === "unavailable") return "存储不可用";
  return "未配置";
}

export function CredentialsSettings() {
  const [items, setItems] = useState<CredentialPublic[]>([]);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [modes, setModes] = useState<Record<string, "persistent" | "process">>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      setItems((await listCredentials()).credentials);
    } catch (reason) {
      setError(message(reason));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function save(item: CredentialPublic) {
    const secret = secrets[item.slot] ?? "";
    if (!secret) return;
    const provider = item.slot === "market_data:tushare" ? "tushare" : item.provider;
    if (!provider || provider === "unknown") {
      setError(`${item.slot} 缺少服务端 provider 标识，暂时不能保存。`);
      return;
    }
    if (!item.target_fingerprint) {
      setError(`${item.slot} 缺少服务端目标指纹；先配置目标，再保存凭据。`);
      return;
    }

    setBusy(item.slot);
    setError("");
    setNotice("");
    try {
      await saveCredential(item.slot, {
        secret,
        provider,
        target_fingerprint: item.target_fingerprint,
        storage_mode: modes[item.slot] ?? item.storage_mode ?? "persistent",
        expected_generation: item.credential_generation,
        idempotency_key: newIdempotencyKey(),
      });
      setNotice(`${item.slot} 已保存并绑定当前服务端目标。保存不代表供应商检测成功。`);
      await load();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setSecrets(current => ({ ...current, [item.slot]: "" }));
      setBusy("");
    }
  }

  async function clear(item: CredentialPublic) {
    setBusy(item.slot);
    setError("");
    setNotice("");
    try {
      const updated = await clearCredential(item.slot, item.credential_generation);
      setItems(current => current.map(existing => existing.slot === item.slot ? updated : existing));
      setSecrets(current => ({ ...current, [item.slot]: "" }));
      setNotice(`${item.slot} 已清除。状态以服务端返回为准；清除后不会复用此前保存的 Key。`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy("");
      await load();
    }
  }

  return <section className="phase6-card credentials-settings">
    <div className="section-heading">
      <div>
        <h2>模型与行情凭据</h2>
        <p className="muted">模型凭据用于明确提交的 Agent 任务，行情凭据用于数据服务。凭据只发送到本地后端，不写入浏览器存储或 URL。浏览器关闭不等于后端进程退出。</p>
      </div>
      <button className="refresh-button" type="button" onClick={() => void load()} disabled={Boolean(busy)}>刷新状态</button>
    </div>
    {error ? <p className="phase6-error" role="alert">{error}</p> : null}
    {notice ? <p className="agent-success" role="status">{notice}</p> : null}
    {!items.length ? <p className="muted">凭据服务未就绪，当前无法读取受保护存储状态。</p> : items.map(item => <article className="phase6-credential" key={item.slot}>
      <div>
        <h3>{item.slot === "model" ? "模型服务" : item.slot === "market_data:tushare" ? "Tushare 行情" : item.slot}</h3>
        <p>{item.provider ?? "提供方未知"} · {stateLabel(item)} · {item.origin === "external_environment" ? "外部环境配置" : item.origin === "managed" ? "由本应用管理" : "无凭据"}{item.masked ? ` · ${item.masked}` : ""}</p>
        <p>保存方式：{item.storage_mode === "persistent" ? "受保护的持久保存" : item.storage_mode === "process" ? "仅当前后端进程内存" : "尚未配置"}</p>
        {item.state === "cleared" ? <p role="status">已清除；重新启用必须重新输入 Key。清除状态不会回退到外部环境变量。</p> : null}
        {item.reason ? <p>{item.reason === "target_mismatch" ? "凭据绑定到旧目标。需按当前服务端目标重新绑定。" : item.reason}</p> : null}
        {!item.target_fingerprint ? <p className="practice-field-hint">服务端尚未提供可绑定目标；配置目标后才能保存凭据。</p> : null}
        <details><summary>查看绑定状态</summary><dl><div><dt>目标指纹</dt><dd>{item.target_fingerprint ?? "尚未设置"}</dd></div><div><dt>凭据版本</dt><dd>{item.credential_generation}</dd></div></dl></details>
      </div>
      <div className="phase6-credential-form">
        <label className="practice-field"><span>新的 Key（保存后清空）</span><input type="password" autoComplete="new-password" value={secrets[item.slot] ?? ""} onChange={event => setSecrets(current => ({ ...current, [item.slot]: event.target.value }))} /></label>
        <label className="practice-field"><span>保存方式</span><select value={modes[item.slot] ?? item.storage_mode ?? "persistent"} onChange={event => setModes(current => ({ ...current, [item.slot]: event.target.value as "persistent" | "process" }))}><option value="persistent">受保护持久保存</option><option value="process">仅当前后端进程</option></select></label>
        <div className="phase6-actions">
          <button className="practice-primary" type="button" onClick={() => void save(item)} disabled={busy === item.slot || !(secrets[item.slot] ?? "") || !item.target_fingerprint}>保存并清空输入</button>
          <button className="link-button" type="button" onClick={() => void clear(item)} disabled={busy === item.slot || item.state === "cleared"}>清除凭据</button>
        </div>
      </div>
    </article>)}
    <p className="practice-field-hint">清除后要重新填写 Key。调用前仍需由相应功能读取披露摘要并取得本次同意。</p>
  </section>;
}
