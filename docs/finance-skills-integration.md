# Finance-Skills Integration

Market uses `himself65/finance-skills` as a source of workflow patterns, not as
a wholesale runtime dependency.

## Applied Plugins

| Upstream skill/plugin | Market implementation | LLM role |
| --- | --- | --- |
| `tradingview-reader` | Read-only OpenCLI provider plus normalized quote, screener, option, news, symbol search, watchlist, alert, and chart-state tables. | None for ingestion. |
| `options-payoff` | `investment_panel.analysis.options_payoff` computes expiry/theoretical payoff curves, breakevens, max gain/loss, and standard scenarios from stored chains. | Parse screenshots/free-form strategies into structured legs only. |
| `earnings-preview`, `earnings-recap`, `estimate-analysis` | `investment_panel.analysis.earnings_setup` scores revision momentum, surprise history, estimate spread, and analyst target sentiment. | Memo prose and transcript/news interpretation only. |
| `company-valuation` | `investment_panel.analysis.valuation` stores DCF base case, relative revenue multiple, and blended valuation rows. | Assumption selection only when structured data is missing. |
| `sepa-strategy`, `stock-liquidity`, `stock-correlation`, `etf-premium` | Existing deterministic analysis modules and yfinance/TradingView ingestion. | None. |

## Deliberately Not Applied

- `finance-social-readers`: social ingestion belongs in Arco/Birdclaw first.
- `funda-data`: paid/external provider; keep optional until Joe explicitly
  changes scope.
- `finance-sentiment`: paid API; Arco should own weak-signal/social synthesis.
- `startup-tools`, `ui-tools`, `skill-creator`: not core Market workflows.

## Review Checklist

- New source rows must write raw payloads plus normalized fields.
- Provider failures must be visible in `provider_runs` / `source_health`.
- Decision queue freshness must not treat documentation as live data.
- Deterministic math must be test-covered before any LLM memo uses it.
- LLM outputs must separate facts from interpretation and cite stored evidence.
