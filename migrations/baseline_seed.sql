-- Initial configuration only. No user facts or signing secrets.
INSERT INTO catalog.instrument VALUES (1, 'QQQ', 'QQQ', 'equity', NULL, NULL, 'option-history', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'America/New_York', NULL, NULL, NULL, NULL);
INSERT INTO catalog.instrument VALUES (2, 'NVDA', 'NVDA', 'equity', NULL, NULL, 'option-history', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'America/New_York', NULL, NULL, NULL, NULL);
INSERT INTO analysis.option_recovery_cohort VALUES ('442fe669-20ec-4e6e-867a-5e6640fae5e4', 'short_horizon_convex_v1', 'options-recovery-v4-pre-reset', CURRENT_TIMESTAMP, 'retired', 5, NULL, '["invalid_reference_bar", "legacy_cohort_quarantined"]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO analysis.option_recovery_cohort VALUES ('57265d74-df6f-4a46-9bdd-da895deb8904', 'short_horizon_convex_v2', 'options-recovery-v5', CURRENT_TIMESTAMP, 'collecting', 5, NULL, '["five_qualified_forward_dates_required", "recovery_paper_actions_disabled"]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO analysis.option_history_canary VALUES (1, 'history-v3-price-shape-r3', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO analysis.option_history_canary VALUES (2, 'history-v3-price-shape-r4-ticket', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO analysis.option_history_canary VALUES (3, 'history-v3-price-shape-r5-contract-terms', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO app.option_history_policy VALUES (1, 'on', 'active', 'core', 15, 'PAPER_READY', 'robinhood', 730, 30, 90, 'options-chain-reliability-20260722', 0, 'core 15-minute QQQ history', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'history_full', NULL, NULL, NULL, 7, 730);
INSERT INTO app.option_history_policy VALUES (2, 'on', 'shadow', 'standard', 60, 'WATCH', 'robinhood', 730, 30, 90, 'options-chain-reliability-20260722', 0, 'early NVDA hourly shadow exception', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'history_full', NULL, NULL, NULL, 7, 730);
INSERT INTO ops.option_quote_partition_policy VALUES ('default', '2026-10-01', 7, 730, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
SELECT pg_catalog.setval('analysis.option_history_canary_id_seq', 3, true);
SELECT pg_catalog.setval('catalog.instrument_id_seq', 2, true);
