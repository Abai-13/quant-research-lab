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

interface SeriesPoint {
  date: string;
  value: number;
}

function pathSeries(path: ComparisonPath): SeriesPoint[] {
  const firstNav = path.points.find((point) => point.nav_cents !== null)?.nav_cents ?? null;
  const points: SeriesPoint[] = [];
  for (const point of path.points) {
    let value = point.return_bps;
    if (value === null && point.nav_cents !== null && firstNav !== null && firstNav > 0) {
      value = Math.round(((point.nav_cents - firstNav) / firstNav) * 10_000);
    }
    if (value !== null && Number.isFinite(value)) {
      points.push({ date: point.date, value });
    }
  }
  return points;
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
  if (!comparison || comparison.paths.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">还没有可绘制的对照曲线</p>
        <p>揭晓第一个交易日后，这里会显示我的操作、系统路径与持有不动的对比。</p>
      </div>
    );
  }

  const paths = comparison.paths.filter((path) => !hideSystem || !isSystemPath(path));
  const series = paths
    .map((path) => ({ path, points: pathSeries(path) }))
    .filter((entry) => entry.points.length > 0);

  if (series.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">暂无可绘制的数据点</p>
        <p>当前对照结果还没有数值；未知估值会保持为空，不按 0 绘制。</p>
      </div>
    );
  }

  const visibleDates = new Set<string>();
  for (const entry of series) {
    for (const point of entry.points) {
      if (!revealedDate || point.date <= revealedDate) {
        visibleDates.add(point.date);
      }
    }
  }
  const dates = Array.from(visibleDates).sort();
  if (dates.length === 0) {
    return (
      <div className="empty-state practice-chart-empty">
        <p className="empty-title">尚未揭晓可绘制区间</p>
        <p>曲线只显示已揭晓日期以内的数据，不会提前画出未来行情。</p>
      </div>
    );
  }

  const dateIndex = new Map(dates.map((date, index) => [date, index]));
  const values = series.flatMap((entry) =>
    entry.points.filter((point) => dateIndex.has(point.date)).map((point) => point.value),
  );
  let minValue = Math.min(...values, 0);
  let maxValue = Math.max(...values, 0);
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
    <div className="practice-chart">
      <div className="practice-chart-legend">
        {paths.map((path) => (
          <span className="practice-legend-item" key={path.key}>
            <span
              className="practice-legend-swatch"
              style={{
                background: PATH_COLORS[path.key] ?? "#49525e",
                opacity: path.key === "hold" ? 0.7 : 1,
              }}
              aria-hidden="true"
            />
            {pathLabel(path)}
          </span>
        ))}
      </div>
      <svg
        className="practice-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="历史模拟对照收益曲线，仅包含已揭晓日期"
      >
        {tickValues.map((value) => {
          const y = yFor(value);
          return (
            <g key={value}>
              <line x1={left} y1={y} x2={width - right} y2={y} stroke="#eef1f4" strokeWidth={1} />
              <text x={left - 8} y={y + 4} textAnchor="end" fontSize={11} fill="#5d6673">
                {formatBps(Math.round(value))}
              </text>
            </g>
          );
        })}
        <line
          x1={left}
          y1={yFor(0)}
          x2={width - right}
          y2={yFor(0)}
          stroke="#cfd5dd"
          strokeWidth={1}
          strokeDasharray="3 4"
        />
        {series.map((entry) => {
          const visible = entry.points.filter((point) => dateIndex.has(point.date));
          if (visible.length === 0) {
            return null;
          }
          const d = visible
            .map((point, index) => `${index === 0 ? "M" : "L"}${xFor(point.date).toFixed(1)},${yFor(point.value).toFixed(1)}`)
            .join(" ");
          return (
            <g key={entry.path.key}>
              <path
                d={d}
                fill="none"
                stroke={PATH_COLORS[entry.path.key] ?? "#49525e"}
                strokeWidth={entry.path.key === "hold" ? 1.5 : 2.2}
                strokeDasharray={PATH_DASH[entry.path.key]}
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity={entry.path.key === "hold" ? 0.75 : 1}
              />
              {visible.map((point) => (
                <circle
                  key={`${entry.path.key}-${point.date}`}
                  cx={xFor(point.date)}
                  cy={yFor(point.value)}
                  r={2.6}
                  fill="#ffffff"
                  stroke={PATH_COLORS[entry.path.key] ?? "#49525e"}
                  strokeWidth={1.6}
                >
                  <title>
                    {`${pathLabel(entry.path)} · ${formatDateShort(point.date)} · ${formatBps(point.value)}`}
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
      {hideSystem ? (
        <p className="muted practice-chart-note">
          系统建议路径已隐藏。你仍可随时显示；系统建议仅供参考，不会自动产生交易。
        </p>
      ) : null}
    </div>
  );
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
              <td>{path.trade_count === null ? "—" : formatShares(path.trade_count)}</td>
              <td>{formatWeightBps(path.turnover_bps)}</td>
              <td>{formatWeightBps(path.concentration_bps)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {paths.some((path) => path.restrictions.length > 0) ? (
        <ul className="practice-restrictions">
          {paths.flatMap((path) =>
            path.restrictions.map((restriction) => (
              <li key={`${path.key}-${restriction.code}`}>
                {pathLabel(path)}：{restriction.code}
                {restriction.detail ? `（${restriction.detail}）` : ""}
              </li>
            )),
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
