# Market context

Use these terms consistently when reading or changing Market.

| Term | Meaning | Primary owner |
|---|---|---|
| Publication | An immutable, versioned application decision/read-model output selected for API use | `database/panel_publications.py`, domain publication modules |
| Read Model | A PostgreSQL query result shaped for a product surface; it is not a provider payload | `database/panel_models.py`, `database/panel_queries.py`, `/api/panel-snapshot` |
| Ingestion Run | One managed collector execution with start, counts, terminal status, and failure details | `database/ingestion.py` |
| Decision Truth | The current evidence-backed state, route verdict, readiness, and execution blocker for a symbol | `database/options_decision_system.py`, `core/event_scout.py` |
| Option History | Point-in-time option capture, history policy, health, evidence, and retention | `database/options_history.py` |
| Option Ticket | A bounded, paper-only candidate with entry, risk, exits, evidence, and blockers | `database/options_execution.py`, `app/actions/options.py` |
| Paper Readiness | A deterministic gate. It must be true before paper execution or promotion can proceed | `database/options_decision_system.py`, `database/options_execution.py` |
| Event Scout | Event signal intake, packet creation, cooldown, replay, and shared decision-truth linkage | `core/event_scout.py`, `core/event_scout_runtime.py`, `app/actions/event_scout.py` |
| Advisory Provider | A structured, advisory-only provider request and validated result | `providers/advisory.py`, option-agent workflow |
| Source Fact | A normalized observation tied to a provider payload manifest and ingestion run | `database/source_facts.py`, `database/payload_archive.py` |

When a change crosses two terms, keep the transaction and policy in the owner
that sequences the workflow. Keep query shaping in a Read Model owner. Start
with the area inventory and no more than three owner interfaces:

```sh
uv run python scripts/architecture_inventory.py --area api
uv run python scripts/architecture_inventory.py --area options
```

Use `make test-api`, `make test-options`, `make test-postgres`, or
`make test-unit` as the focused verification command for the selected owner.
Generated schemas, bundles, and full logs are build outputs, not navigation
documents.
