-- Indexes: app schema.

CREATE INDEX ix_app_alert_decision_id ON app.alert USING btree (decision_id);

CREATE INDEX ix_app_catalyst_current_starts_at ON app.catalyst USING btree (starts_at) WHERE (status = 'current'::text);

CREATE INDEX ix_app_current_publication_item_rank ON app.current_publication_item USING btree (scope, model_name, rank);

CREATE INDEX ix_app_paper_order_decision_id ON app.paper_order USING btree (decision_id);

CREATE INDEX ix_app_paper_order_generic_execution ON app.paper_order USING btree (lane, status, created_at, id) WHERE (event_id IS NULL);

CREATE INDEX ix_app_paper_order_lane_status_created ON app.paper_order USING btree (lane, status, created_at DESC);

CREATE INDEX ix_app_paper_order_ticker_revision ON app.paper_order USING btree (ticker_decision_revision, expression_kind, status, created_at DESC);

CREATE INDEX ix_app_portfolio_transaction_instrument_time ON app.portfolio_transaction USING btree (instrument_id, executed_at, created_at);

CREATE INDEX ix_app_portfolio_transaction_recent ON app.portfolio_transaction USING btree (executed_at DESC);

CREATE INDEX ix_app_publication_analysis_run_id ON app.publication USING btree (analysis_run_id);

CREATE INDEX ix_app_publication_bundle ON app.publication USING btree (bundle_id) WHERE (bundle_id IS NOT NULL);

CREATE INDEX ix_app_publication_bundle_item_rank ON app.publication_bundle_item USING btree (bundle_id, model_name, rank);

CREATE INDEX ix_app_publication_item_rank ON app.publication_item USING btree (publication_id, model_name, rank);

CREATE INDEX ix_app_publication_latest ON app.publication USING btree (scope, published_at DESC) WHERE (status = 'published'::text);

CREATE INDEX ix_app_publication_scope_status_created ON app.publication USING btree (scope, status, published_at DESC, created_at DESC) INCLUDE (id, analysis_run_id);

CREATE INDEX ix_app_thesis_assessment_revision ON app.thesis_evidence_assessment USING btree (thesis_revision_id, stance, materiality);

CREATE INDEX ix_app_thesis_automation_run_symbol ON app.thesis_automation_run USING btree (instrument_id, started_at DESC);

CREATE INDEX ix_app_thesis_expression_revision ON app.thesis_expression USING btree (thesis_revision_id, expression_kind);

CREATE INDEX ix_app_thesis_instrument_revision ON app.thesis USING btree (instrument_id, revision DESC);

CREATE INDEX ix_app_thesis_review_event_symbol ON app.thesis_review_event USING btree (instrument_id, created_at DESC);

CREATE INDEX ix_app_trade_journal_decision_id ON app.trade_journal USING btree (decision_id);

CREATE INDEX ix_decision_inbox_active_created ON app.decision_inbox_item USING btree (status, created_at DESC, id DESC);

CREATE INDEX ix_decision_inbox_opportunity ON app.decision_inbox_item USING btree (opportunity_id, ticket_version, event_type);

CREATE INDEX ix_decision_inbox_user_state ON app.decision_inbox_item USING btree (user_state, snoozed_until, created_at DESC);

CREATE INDEX ix_decision_truth_as_of ON app.decision_truth USING btree (as_of DESC, symbol);

CREATE INDEX ix_manual_account_snapshot_effective ON app.manual_account_snapshot USING btree (account_key, effective_at DESC, id DESC);

CREATE INDEX ix_notification_outbox_due ON app.notification_outbox USING btree (status, next_attempt_at, created_at) WHERE (status = ANY (ARRAY['queued'::text, 'failed'::text]));

CREATE INDEX ix_option_history_policy_event_active ON app.option_history_policy USING btree (profile, event_id, effective_state, expires_at);

CREATE INDEX ix_option_history_policy_state ON app.option_history_policy USING btree (requested_state, effective_state, collection_tier);

CREATE INDEX ix_recovery_paper_order_cohort ON app.paper_order USING btree (cohort_id, status, created_at DESC) WHERE (cohort_id IS NOT NULL);

CREATE INDEX ix_recovery_paper_order_signal ON app.paper_order USING btree (event_signal_id, status, created_at DESC) WHERE (event_signal_id IS NOT NULL);

CREATE UNIQUE INDEX uq_app_catalyst_current_event_key ON app.catalyst USING btree (event_key) WHERE (status = 'current'::text);

CREATE UNIQUE INDEX uq_app_publication_one_published_scope ON app.publication USING btree (scope) WHERE (status = 'published'::text);

CREATE UNIQUE INDEX uq_app_thesis_current ON app.thesis USING btree (instrument_id) WHERE (status = 'current'::text);

CREATE UNIQUE INDEX uq_app_thesis_expression_active_kind ON app.thesis_expression USING btree (thesis_revision_id, expression_kind) WHERE (status = 'active'::text);

CREATE UNIQUE INDEX uq_recovery_paper_order_event_family ON app.paper_order USING btree (event_id, strategy_family) WHERE ((event_id IS NOT NULL) AND (strategy_family IS NOT NULL));

CREATE UNIQUE INDEX ux_app_portfolio_transaction_reversal ON app.portfolio_transaction USING btree (reverses_transaction_id) WHERE (reverses_transaction_id IS NOT NULL);
