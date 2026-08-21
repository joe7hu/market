# Market context

Use these terms consistently when reading or changing Market.

| Term | Meaning | Primary owner |
|---|---|---|
| Publication | An immutable, versioned application decision/read-model output selected for API use | `database/panel_publications.py`, domain publication modules |
| Read Model | A PostgreSQL query result shaped for a product surface; it is not a provider payload | `database/panel_models.py`, `database/panel_queries.py` |
| Ingestion Run | One managed collector execution with start, counts, terminal status, and failure details | `database/ingestion.py` |
| Decision Truth | The current evidence-backed state, route verdict, readiness, and execution blocker for a symbol | `database/options_decision_system.py`, `database/event_scout.py` |
| Option Ticket | A bounded, paper-only candidate with entry, risk, exits, evidence, and blockers | `database/options_history_ticket.py`, `app/actions/options.py` |
| Paper Readiness | A deterministic gate. It must be true before paper execution or promotion can proceed | options decision and paper execution owners |
| Event Scout | Event signal intake, packet creation, cooldown, replay, and shared decision-truth linkage | `database/event_scout.py`, `app/actions/event_scout.py` |
| Source Fact | A normalized observation tied to a provider payload manifest and ingestion run | `database/source_facts.py`, `database/payload_archive.py` |

When a change crosses two terms, keep the transaction and policy in the owner
that sequences the workflow. Keep query shaping in a read-model owner.
