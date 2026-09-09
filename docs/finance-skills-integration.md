# Finance-Skills Integration

Market uses `himself65/finance-skills` as a source of workflow patterns, not as
a wholesale runtime dependency.

## Applied Plugins

| Upstream skill/plugin | Market implementation | LLM role |
| --- | --- | --- |
| `tradingview-reader` | Read-only OpenCLI provider plus normalized quote, screener, option, news, and exchange-qualified instrument identity. | None for ingestion. |
| `options-payoff` | `investment_panel.domain.options.options_payoff` computes expiry/theoretical payoff curves, breakevens, max gain/loss, and standard scenarios from stored chains. | Parse screenshots/free-form strategies into structured legs only. |
| `earnings-preview`, `earnings-recap`, `estimate-analysis` | The typed `valuations`, `estimates`, and `disclosures` read models feed `investment_panel.domain.decision`. | Memo prose and transcript/news interpretation only. |
| `company-valuation` | The current valuation read model and decision contracts under `investment_panel.domain.decision` own structured valuation context. | Assumption selection only when structured data is missing. |
| `sepa-strategy`, `stock-liquidity`, `stock-correlation` | Deterministic factor/research owners under `investment_panel.domain`, with provider ingestion under `investment_panel.infrastructure`. | None. |

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
