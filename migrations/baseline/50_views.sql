-- Read-only views.

CREATE VIEW analysis.strategy_registry AS
 SELECT revision.id AS strategy_revision_id,
    revision.strategy_key,
    revision.revision,
    revision.name,
    revision.status,
    revision.mechanism_class,
    revision.economic_mechanism,
    revision.falsification_rule,
    revision.source_definition_version,
    revision.strategy_family,
    revision.promotability,
    revision.actionability,
    revision.p3_enabled,
    revision.parameters,
    revision.created_at,
    revision.promoted_at,
    revision.supersedes_id,
    manifest.manifest_hash,
    manifest.available_at AS manifest_available_at
   FROM (analysis.strategy_revision revision
     LEFT JOIN analysis.strategy_manifest manifest ON ((manifest.strategy_revision_id = revision.id)));

CREATE VIEW analysis.strategy_trial_accounting AS
 SELECT family.family_key,
    trial.id AS research_trial_id,
    trial.trial_key,
    trial.status,
    trial.input_cutoff,
    trial.available_at,
    manifest.expected_member_count,
    count(observation.id) AS observed_member_count,
    count(observation.id) FILTER (WHERE (observation.outcome <> '{}'::jsonb)) AS outcome_member_count,
    (EXISTS ( SELECT 1
           FROM analysis.trial_result result
          WHERE (result.research_trial_id = trial.id))) AS has_result,
    analysis.research_trial_p3_denominator_complete(trial.id) AS denominator_complete,
    analysis.research_family_complete(trial.experiment_family_id) AS family_complete
   FROM (((analysis.research_trial trial
     JOIN analysis.experiment_family family ON ((family.id = trial.experiment_family_id)))
     LEFT JOIN analysis.trial_universe_manifest manifest ON ((manifest.research_trial_id = trial.id)))
     LEFT JOIN analysis.universe_observation observation ON ((observation.research_trial_id = trial.id)))
  GROUP BY family.family_key, trial.id, trial.trial_key, trial.status, trial.input_cutoff, trial.available_at, manifest.expected_member_count, trial.experiment_family_id;

CREATE VIEW app.publication_content_item AS
 SELECT item.publication_id,
    item.model_name,
    item.stable_key,
    item.rank,
    item.instrument_id,
    item.payload
   FROM (app.publication_item item
     JOIN app.publication publication ON ((publication.id = item.publication_id)))
  WHERE (publication.bundle_id IS NULL)
UNION ALL
 SELECT publication.id AS publication_id,
    item.model_name,
    item.stable_key,
    item.rank,
    item.instrument_id,
    payload.payload
   FROM ((app.publication publication
     JOIN app.publication_bundle_item item ON ((item.bundle_id = publication.bundle_id)))
     JOIN app.publication_payload payload ON ((payload.content_hash = item.content_hash)));

CREATE VIEW raw.confirmed_price_bar AS
 SELECT id,
    instrument_id,
    source_id,
    ingest_run_id,
    payload_id,
    "interval",
    trading_date,
    observed_at,
    open,
    high,
    low,
    close,
    volume,
    currency,
    available_at
   FROM raw.confirmed_price_bar_at(now(), NULL::bigint[]) confirmed_price_bar_at(id, instrument_id, source_id, ingest_run_id, payload_id, "interval", trading_date, observed_at, open, high, low, close, volume, currency, available_at);

CREATE VIEW raw.confirmed_quote AS
 SELECT id,
    instrument_id,
    source_id,
    ingest_run_id,
    payload_id,
    observed_at,
    price,
    change_abs,
    change_pct,
    currency,
    available_at
   FROM raw.confirmed_quote_at(now(), NULL::bigint[]) confirmed_quote_at(id, instrument_id, source_id, ingest_run_id, payload_id, observed_at, price, change_abs, change_pct, currency, available_at);
