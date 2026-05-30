import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

export type DataSourceState = "live" | "empty";
export type Tone = "good" | "warn" | "bad" | "info" | "muted";

export function PageFrame({
  eyebrow,
  title,
  subtitle,
  action,
  children,
}: {
  eyebrow?: string;
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="page-frame">
      <header className="page-header">
        <div>
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          {title && <h1>{title}</h1>}
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

export function Panel({ title, children, className = "", headerAction }: { title: string; children: ReactNode; className?: string; headerAction?: ReactNode }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <h2>{title}</h2>
        {headerAction && <span>{headerAction}</span>}
      </header>
      {children}
    </section>
  );
}

export function SourceNotice({ items }: { items: Array<[string, DataSourceState]> }) {
  return (
    <section className="source-notice" aria-label="Data source status">
      {items.map(([label, state]) => (
        <span key={label}>
          {label}
          <SourcePill state={state} />
        </span>
      ))}
    </section>
  );
}

export function SourcePill({ state }: { state: DataSourceState }) {
  const label = state === "live" ? "Rows loaded" : "No rows";
  return <i className={`source-pill ${state}`}>{label}</i>;
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

export function MetricStrip({ metrics }: { metrics: Array<[string, string, string, Tone | string]> }) {
  return (
    <section className="metric-strip">
      {metrics.map(([label, value, caption, tone]) => (
        <div key={label} className={`metric-box ${tone}`}>
          <span>{label}</span>
          <strong>{value}</strong>
          <small>{caption}</small>
        </div>
      ))}
    </section>
  );
}

export function MetricBadge({ label, value, caption, tone = "info" }: { label: string; value: string; caption?: string; tone?: Tone }) {
  return (
    <div className={`metric-badge ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {caption && <small>{caption}</small>}
    </div>
  );
}

export function TabBar({ tabs, active, onSelect }: { tabs: string[]; active?: string; onSelect?: (tab: string) => void }) {
  return (
    <div className="tab-bar">
      {tabs.map((tab, index) => <button key={tab} className={(active ?? tabs[0]) === tab || (!active && index === 0) ? "active" : ""} type="button" onClick={() => onSelect?.(tab)}>{tab}</button>)}
    </div>
  );
}

export function SegmentedControl({ options, value, onChange }: { options: string[]; value: string; onChange: (value: string) => void }) {
  return (
    <div className="segmented-control">
      {options.map((option) => (
        <button key={option} className={value === option ? "active" : ""} type="button" onClick={() => onChange(option)}>{option}</button>
      ))}
    </div>
  );
}

export function TableFrame({ children }: { children: ReactNode }) {
  return <div className="table-wrap">{children}</div>;
}

export function TextLink({ children }: { children: ReactNode }) {
  return <button className="text-link" type="button">{children} <ChevronRight size={13} /></button>;
}

export function GhostButton({ children, disabled = false, title }: { children: ReactNode; disabled?: boolean; title?: string }) {
  return <button className="ghost-button" type="button" disabled={disabled} title={title}>{children}</button>;
}

export function IconButton({ children, label, onClick }: { children: ReactNode; label: string; onClick?: () => void }) {
  return <button className="icon-button" type="button" aria-label={label} title={label} onClick={onClick}>{children}</button>;
}

export function FilterRail({
  compact = false,
  decision = "",
  decisionLabel = "Decision",
  decisions = [],
  tickerQuery = "",
  minScore = 0,
  assetClass = "",
  assetClasses = [],
  minConfidence = 0,
  source = "",
  sources = [],
  freshness = "",
  freshnessOptions = [],
  sourceCluster = "",
  sourceClusters = [],
  catalystFilter = "",
  liquidityFilter = "",
  ownership = "",
  investorQuery = "",
  onDecision,
  onTickerQuery,
  onMinScore,
  onAssetClass,
  onMinConfidence,
  onSource,
  onFreshness,
  onSourceCluster,
  onCatalystFilter,
  onLiquidityFilter,
  onOwnership,
  onInvestorQuery,
  onReset,
}: {
  compact?: boolean;
  decision?: string;
  decisionLabel?: string;
  decisions?: string[];
  tickerQuery?: string;
  minScore?: number;
  assetClass?: string;
  assetClasses?: string[];
  minConfidence?: number;
  source?: string;
  sources?: string[];
  freshness?: string;
  freshnessOptions?: string[];
  sourceCluster?: string;
  sourceClusters?: string[];
  catalystFilter?: string;
  liquidityFilter?: string;
  ownership?: string;
  investorQuery?: string;
  onDecision?: (value: string) => void;
  onTickerQuery?: (value: string) => void;
  onMinScore?: (value: number) => void;
  onAssetClass?: (value: string) => void;
  onMinConfidence?: (value: number) => void;
  onSource?: (value: string) => void;
  onFreshness?: (value: string) => void;
  onSourceCluster?: (value: string) => void;
  onCatalystFilter?: (value: string) => void;
  onLiquidityFilter?: (value: string) => void;
  onOwnership?: (value: string) => void;
  onInvestorQuery?: (value: string) => void;
  onReset?: () => void;
}) {
  return (
    <aside className="filter-rail">
      <div className="rail-title">
        <strong>Filters</strong>
        <button type="button" onClick={onReset}>Reset</button>
      </div>
      {(!compact || decisions.length > 0) && (
        <label>
          <span>{decisionLabel}</span>
          <select value={decision} onChange={(event) => onDecision?.(event.target.value)}>
            <option value="">All</option>
            {decisions.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      )}
      <label>
        <span>Ticker</span>
        <input value={tickerQuery} onChange={(event) => onTickerQuery?.(event.target.value)} placeholder="Any" />
      </label>
      {compact && (
        <label>
          <span>Investor</span>
          <input value={investorQuery} onChange={(event) => onInvestorQuery?.(event.target.value)} placeholder="Any" />
        </label>
      )}
      {!compact && (
        <label>
          <span>Score Range</span>
          <input type="range" min="0" max="100" value={minScore} onChange={(event) => onMinScore?.(Number(event.target.value))} />
          <small>{minScore}+ minimum</small>
        </label>
      )}
      {!compact && (
        <label>
          <span>Asset</span>
          <select value={assetClass} onChange={(event) => onAssetClass?.(event.target.value)}>
            <option value="">All</option>
            {assetClasses.map((item) => <option key={item} value={item}>{titleLabel(item)}</option>)}
          </select>
        </label>
      )}
      {!compact && (
        <label>
          <span>Confidence</span>
          <input type="range" min="0" max="100" value={minConfidence} onChange={(event) => onMinConfidence?.(Number(event.target.value))} />
          <small>{minConfidence}+ minimum</small>
        </label>
      )}
      {!compact && (
        <label>
          <span>Signal Source</span>
          <select value={source} onChange={(event) => onSource?.(event.target.value)}>
            <option value="">All</option>
            {sources.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
      )}
      {!compact && freshnessOptions.length > 0 && (
        <label>
          <span>Freshness</span>
          <select value={freshness} onChange={(event) => onFreshness?.(event.target.value)}>
            <option value="">All</option>
            {freshnessOptions.map((item) => <option key={item} value={item}>{titleLabel(item)}</option>)}
          </select>
        </label>
      )}
      {!compact && sourceClusters.length > 0 && (
        <label>
          <span>Source Cluster</span>
          <select value={sourceCluster} onChange={(event) => onSourceCluster?.(event.target.value)}>
            <option value="">All</option>
            {sourceClusters.map((item) => <option key={item} value={item}>{titleLabel(item)}</option>)}
          </select>
        </label>
      )}
      {!compact && (
        <label>
          <span>Catalyst</span>
          <select value={catalystFilter} onChange={(event) => onCatalystFilter?.(event.target.value)}>
            <option value="">All</option>
            <option value="has">Has window</option>
            <option value="none">No window</option>
          </select>
        </label>
      )}
      {!compact && (
        <label>
          <span>Liquidity</span>
          <select value={liquidityFilter} onChange={(event) => onLiquidityFilter?.(event.target.value)}>
            <option value="">All</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
      )}
      {!compact && (
        <label>
          <span>Ownership</span>
          <select value={ownership} onChange={(event) => onOwnership?.(event.target.value)}>
            <option value="">All</option>
            <option value="owned">Owned</option>
            <option value="unowned">Unowned</option>
          </select>
        </label>
      )}
      <button className="primary-button" type="button" onClick={onReset}>Reset Filters</button>
    </aside>
  );
}

function titleLabel(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
