import { useId, useState } from "react";
import type { Comparison, ComparisonPath, MarketSeries } from "./types";
import {
  formatBps,
  formatCents,
  formatDateShort,
  formatShares,
  formatWeightBps,
  pathLabel,
} from "./format";

const PATH_COLORS: Record<string, string> = {
  user: "#2456c4",
  incremental: "#0f766e",
  independent: "#b45309",
  hold: "#8b95a5",
};

const PATH_DASH: Record<string, string | undefined> = {
  hold: "6 5",
};

function isSystemPath(path: ComparisonPath): boolean {
  return path.key === "incremental" || path.key === "independent";
}

export function ComparisonChart({
  comparison,
  revealedDate,
  hideSystem,
}: {
  comparison: Comparison | null;
  revealedDate: string | null;
  hideSystem: boolean;
}) {
  const [metric, setMetric] = useState<"nav" | "return">("nav");
  const metricName = useId();
  const [hiddenPaths, setHiddenPaths] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState("");
  if (!comparison || comparison.paths.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">还没有可绘制的对照曲线</p>
        <p>揭晓第一个交易日后，这里会显示我的操作、系统路径与持有不动的对比。</p>
      </div>
    );
  }

  const paths = comparison.paths;
  const availablePaths = paths.filter((path) => !hideSystem || !isSystemPath(path));
  const dates = Array.from(new Set(paths.flatMap((path) => path.points
    .filter((point) => !revealedDate || point.date <= revealedDate)
    .map((point) => point.date)))).sort();
  if (dates.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">尚未揭晓可绘制区间</p>
        <p>曲线只显示已揭晓日期以内的数据，不会提前画出未来行情。</p>
      </div>
    );
  }

  const visiblePaths = availablePaths.filter((path) => !hiddenPaths.includes(path.key));
  const valueFor = (path: ComparisonPath, date: string): number | null => {
    const point = path.points.find((item) => item.date === date);
    const value = metric === "nav" ? point?.nav_cents : point?.return_bps;
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const values = visiblePaths.flatMap((path) => dates.map((date) => valueFor(path, date)).filter((value): value is number => value !== null));
  const selected = dates.includes(selectedDate) ? selectedDate : dates[dates.length - 1];
  const plot = (() => {
    if (values.length === 0) {
      return <p className="muted" role="status">{visiblePaths.length === 0
        ? "所有路径已隐藏，可通过上方图例重新显示；逐日数据仍可在下方表格查看。"
        : `当前路径没有可显示的${metric === "nav" ? "净资产" : "服务端收益百分比"}；未知值保持为空，不按 0 绘制。`}</p>;
    }
    let minValue = Math.min(...values, ...(metric === "return" ? [0] : []));
    let maxValue = Math.max(...values, ...(metric === "return" ? [0] : []));
    if (minValue === maxValue) {
      minValue -= 100;
      maxValue += 100;
    }
    const pad = Math.max((maxValue - minValue) * 0.12, 20);
    minValue -= pad;
    maxValue += pad;

    const width = 760;
    const height = 300;
    const left = 58;
    const right = 18;
    const top = 18;
    const bottom = 34;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const dateIndex = new Map(dates.map((date, index) => [date, index]));
    const xFor = (date: string): number => {
      const index = dateIndex.get(date) ?? 0;
      return left + (dates.length === 1 ? plotWidth / 2 : (index / (dates.length - 1)) * plotWidth);
    };
    const yFor = (value: number): number =>
      top + ((maxValue - value) / (maxValue - minValue)) * plotHeight;

    const ticks = 4;
    const tickValues = Array.from({ length: ticks + 1 }, (_, index) =>
      minValue + ((maxValue - minValue) * index) / ticks,
    );

    const xLabelIndexes = Array.from(
      new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1]),
    ).filter((index) => index >= 0);

    return (
      <svg
        className="practice-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`历史模拟对照${metric === "nav" ? "净资产" : "服务端收益百分比"}曲线，仅包含已揭晓日期；未知值保持断点`}
      >
        {tickValues.map((value) => {
          const y = yFor(value);
          return (
            <g key={value}>
              <line x1={left} y1={y} x2={width - right} y2={y} stroke="#eef1f4" strokeWidth={1} />
              <text x={left - 8} y={y + 4} textAnchor="end" fontSize={11} fill="#5d6673">
                {metric === "nav" ? formatCents(value) : formatBps(Math.round(value))}
              </text>
            </g>
          );
        })}
        {metric === "return" ? <line x1={left} y1={yFor(0)} x2={width - right} y2={yFor(0)} stroke="#cfd5dd" strokeWidth={1} strokeDasharray="3 4" /> : null}
        {visiblePaths.map((path) => {
          const d = dates.map((date, index) => {
            const value = valueFor(path, date);
            if (value === null) return "";
            const previous = index > 0 ? valueFor(path, dates[index - 1]) : null;
            return `${index === 0 || previous === null ? "M" : "L"}${xFor(date).toFixed(1)},${yFor(value).toFixed(1)}`;
          }).filter(Boolean).join(" ");
          if (!d) return null;
          const points = dates
            .map((date) => ({ date, value: valueFor(path, date) }))
            .filter((point): point is { date: string; value: number } => point.value !== null);
          return (
            <g key={path.key}>
              <path
                d={d}
                fill="none"
                stroke={PATH_COLORS[path.key] ?? "#49525e"}
                strokeWidth={path.key === "hold" ? 1.5 : 2.2}
                strokeDasharray={PATH_DASH[path.key]}
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity={path.key === "hold" ? 0.75 : 1}
              />
              {points.map((point) => (
                <circle
                  key={`${path.key}-${point.date}`}
                  cx={xFor(point.date)}
                  cy={yFor(point.value as number)}
                  r={2.6}
                  fill="#ffffff"
                  stroke={PATH_COLORS[path.key] ?? "#49525e"}
                  strokeWidth={1.6}
                >
                  <title>
                    {`${pathLabel(path)} · ${formatDateShort(point.date)} · ${metric === "nav" ? `${formatCents(point.value)} 元` : formatBps(point.value)}`}
                  </title>
                </circle>
              ))}
            </g>
          );
        })}
        {xLabelIndexes.map((index) => (
          <text
            key={dates[index]}
            x={xFor(dates[index])}
            y={height - 10}
            textAnchor={index === 0 ? "start" : index === dates.length - 1 ? "end" : "middle"}
            fontSize={11}
            fill="#5d6673"
          >
            {formatDateShort(dates[index])}
          </text>
        ))}
      </svg>
    );
  })();

  return (
    <div className="practice-chart">
      <MetricControls name={metricName} metric={metric} onChange={setMetric} />
      <div className="practice-chart-legend">
        {availablePaths.map((path) => (
          <button className="practice-legend-item" type="button" key={path.key}
            aria-pressed={!hiddenPaths.includes(path.key) && (!hideSystem || !isSystemPath(path))}
            disabled={hideSystem && isSystemPath(path)}
            onClick={() => setHiddenPaths((current) => current.includes(path.key) ? current.filter((key) => key !== path.key) : [...current, path.key])}>
            <span
              className="practice-legend-swatch"
              style={{
                background: PATH_COLORS[path.key] ?? "#49525e",
                opacity: path.key === "hold" ? 0.7 : 1,
              }}
              aria-hidden="true"
            />
            <span>{pathLabel(path)}</span>{hideSystem && isSystemPath(path) ? <span>（已隐藏）</span> : null}
          </button>
        ))}
      </div>
      <label className="practice-chart-date-control"><span>查看日期</span>
        <select value={selected} onChange={(event) => setSelectedDate(event.target.value)}>
          {dates.map((date) => <option key={date} value={date}>{formatDateShort(date)}</option>)}
        </select>
      </label>
      <p className="practice-chart-reading" aria-live="polite">
        {formatDateShort(selected)}：{visiblePaths.map((path) => `${pathLabel(path)} ${metric === "nav" ? `${formatCents(valueFor(path, selected))} 元` : formatBps(valueFor(path, selected))}`).join("；") || "没有显示中的路径"}
      </p>
      {plot}
      <details className="practice-chart-data">
        <summary>查看逐日数据表（适合窄屏与辅助技术）</summary>
        <div className="practice-table-wrap"><table className="practice-table">
          <caption className="practice-table-caption">{metric === "nav" ? "净资产（元）" : "服务端收益（%）"}；未知值保持为空</caption>
          <thead><tr><th scope="col">已揭晓日</th>{availablePaths.map((path) => <th scope="col" key={path.key}>{pathLabel(path)}</th>)}</tr></thead>
          <tbody>{dates.map((date) => <tr key={date}><th scope="row">{formatDateShort(date)}</th>{availablePaths.map((path) => <td key={path.key}>{valueFor(path, date) === null ? "未知" : metric === "nav" ? formatCents(valueFor(path, date)) : formatBps(valueFor(path, date))}</td>)}</tr>)}</tbody>
        </table></div>
      </details>
      {availablePaths.some((path) => !path.points.some((point) => metric === "nav" ? point.nav_cents !== null : point.return_bps !== null)) ? <ul className="practice-restrictions">{availablePaths.filter((path) => !path.points.some((point) => metric === "nav" ? point.nav_cents !== null : point.return_bps !== null)).map((path) => <li key={path.key}>{pathLabel(path)}：当前没有可显示的{metric === "nav" ? "净资产" : "服务端收益百分比"}。</li>)}</ul> : null}
      {hideSystem ? (
        <p className="muted practice-chart-note">
          系统建议路径已隐藏。你仍可随时显示；系统建议仅供参考，不会自动产生交易。
        </p>
      ) : null}
    </div>
  );
}

function MetricControls({ name, metric, onChange }: { name: string; metric: "nav" | "return"; onChange: (metric: "nav" | "return") => void }) {
  return <fieldset className="practice-chart-metric"><legend>纵轴单位</legend>
    <label><input type="radio" name={name} checked={metric === "nav"} onChange={() => onChange("nav")} /> 净资产（元）</label>
    <label><input type="radio" name={name} checked={metric === "return"} onChange={() => onChange("return")} /> 服务端收益（%）</label>
  </fieldset>;
}

export function PathMetrics({ comparison, hideSystem }: { comparison: Comparison | null; hideSystem: boolean }) {
  if (!comparison) {
    return null;
  }
  const paths = comparison.paths.filter((path) => !hideSystem || !isSystemPath(path));
  if (paths.length === 0) {
    return null;
  }
  return (
    <div className="practice-table-wrap">
      <table className="practice-table">
        <caption className="practice-table-caption">截至当前揭晓日的路径指标</caption>
        <thead>
          <tr>
            <th scope="col">路径</th>
            <th scope="col">估值（元）</th>
            <th scope="col">收益</th>
            <th scope="col">最大回撤</th>
            <th scope="col">费用（元）</th>
            <th scope="col">交易次数</th>
            <th scope="col">换手</th>
            <th scope="col">集中度</th>
          </tr>
        </thead>
        <tbody>
          {paths.map((path) => (
            <tr key={path.key}>
              <th scope="row">{pathLabel(path)}</th>
              <td>{formatCents(path.nav_cents)}</td>
              <td>{formatBps(path.return_bps)}</td>
              <td>{formatBps(path.drawdown_bps === null ? null : -Math.abs(path.drawdown_bps))}</td>
              <td>{formatCents(path.cost_cents)}</td>
              <td>{path.trade_count === null ? "未知" : formatShares(path.trade_count)}</td>
              <td>{formatWeightBps(path.turnover_bps)}</td>
              <td>{formatWeightBps(path.concentration_bps)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {paths.some((path) => path.restrictions.length > 0 || path.trade_count === 0) ? (
        <ul className="practice-restrictions">
          {paths.flatMap((path) =>
            [
              ...path.restrictions.map((restriction) => (
                <li key={`${path.key}-${restriction.code}`}>
                  {pathLabel(path)}：{restriction.code}
                  {restriction.detail ? `（${restriction.detail}）` : ""}
                </li>
              )),
              ...(path.trade_count === 0 ? [<li key={`${path.key}-zero-trades`}>{pathLabel(path)}：服务端记录为 0 笔交易；这条路径在本模拟区间内没有形成订单成交。</li>] : []),
            ],
          )}
        </ul>
      ) : null}
    </div>
  );
}

export function MarketChart({ market }: { market: MarketSeries }) {
  const points = market.points.filter((point) => point.close !== null);
  if (points.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">该证券在已揭晓区间内没有可用行情</p>
        <p>缺少价格时不绘制曲线，也不会用未来行情补齐。</p>
      </div>
    );
  }
  const values = points.map((point) => point.close as number);
  let minValue = Math.min(...values);
  let maxValue = Math.max(...values);
  if (minValue === maxValue) {
    minValue *= 0.98;
    maxValue *= 1.02;
  }
  const pad = (maxValue - minValue) * 0.1;
  minValue -= pad;
  maxValue += pad;
  const width = 760;
  const height = 220;
  const left = 58;
  const right = 18;
  const top = 16;
  const bottom = 30;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const xFor = (index: number): number =>
    left + (points.length === 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const yFor = (value: number): number =>
    top + ((maxValue - value) / (maxValue - minValue)) * plotHeight;
  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"}${xFor(index).toFixed(1)},${yFor(point.close as number).toFixed(1)}`)
    .join(" ");
  const xLabelIndexes = Array.from(
    new Set([0, Math.floor((points.length - 1) / 2), points.length - 1]),
  ).filter((index) => index >= 0);
  return (
    <div className="practice-chart">
      <svg
        className="practice-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${market.code} 已揭晓收盘价曲线`}
      >
        {[0, 0.5, 1].map((ratio) => {
          const value = minValue + (maxValue - minValue) * (1 - ratio);
          const y = top + plotHeight * ratio;
          return (
            <g key={ratio}>
              <line x1={left} y1={y} x2={width - right} y2={y} stroke="#eef1f4" strokeWidth={1} />
              <text x={left - 8} y={y + 4} textAnchor="end" fontSize={11} fill="#5d6673">
                {value.toFixed(2)}
              </text>
            </g>
          );
        })}
        <path d={path} fill="none" stroke="#2456c4" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {points.map((point, index) => (
          <circle
            key={point.date}
            cx={xFor(index)}
            cy={yFor(point.close as number)}
            r={2.4}
            fill="#ffffff"
            stroke="#2456c4"
            strokeWidth={1.5}
          >
            <title>{`${formatDateShort(point.date)} · 收盘 ${(point.close as number).toFixed(2)}`}</title>
          </circle>
        ))}
        {xLabelIndexes.map((index) => (
          <text
            key={points[index].date}
            x={xFor(index)}
            y={height - 8}
            textAnchor={index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}
            fontSize={11}
            fill="#5d6673"
          >
            {formatDateShort(points[index].date)}
          </text>
        ))}
      </svg>
      {market.limitations.length > 0 ? (
        <ul className="practice-restrictions">
          {market.limitations.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
