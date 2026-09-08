-- Indexes: raw schema.

CREATE INDEX ix_market_observation_pit ON raw.market_observation USING btree (field_name, observed_at, available_at);

CREATE INDEX ix_market_observation_source ON raw.market_observation USING btree (source_id, available_at DESC);

CREATE INDEX ix_raw_content_item_observed ON raw.content_item USING btree (observed_at DESC);

CREATE INDEX ix_raw_fundamental_observation_payload ON raw.fundamental_observation USING btree (payload_id) WHERE (payload_id IS NOT NULL);

CREATE INDEX ix_raw_market_event_point_in_time ON raw.market_event USING btree (event_kind, starts_at, available_at);

CREATE INDEX ix_raw_market_event_version_point_in_time ON raw.market_event_version USING btree (event_kind, starts_at, available_at);

CREATE INDEX ix_raw_option_capture_generation_snapshot_state ON raw.option_capture_generation USING btree (snapshot_id, capture_state, generation DESC);

CREATE INDEX ix_raw_option_quote_contract ON raw.option_quote USING btree (contract_id, observed_at DESC) INCLUDE (mid, provider_iv, underlying_price);

CREATE INDEX ix_raw_option_quote_generation_group ON raw.option_quote USING btree (capture_generation_id, capture_group_key, contract_id);

CREATE INDEX ix_raw_option_quote_history_chain ON raw.option_quote USING btree (snapshot_id, contract_id) INCLUDE (provider_iv, bid, ask, volume, open_interest);

CREATE INDEX ix_raw_option_snapshot_history_lookup ON raw.option_snapshot USING btree (history_symbol, collection_profile, capture_state, slot_at DESC);

CREATE INDEX ix_raw_price_bar_history_asof ON raw.price_bar_history USING btree (instrument_id, trading_date, available_at DESC);

CREATE INDEX ix_raw_price_bar_history_panel_latest ON raw.price_bar_history USING btree (instrument_id, "interval", trading_date DESC, observed_at DESC) INCLUDE (close, available_at, source_id);

CREATE INDEX ix_raw_price_bar_lookup ON raw.price_bar USING btree (instrument_id, trading_date DESC) INCLUDE (close, volume);

CREATE INDEX ix_raw_quote_history_asof ON raw.quote_history USING btree (instrument_id, observed_at, available_at DESC);

CREATE INDEX ix_raw_quote_history_panel_latest ON raw.quote_history USING btree (instrument_id, observed_at DESC, available_at DESC) INCLUDE (price, change_pct, change_abs, source_id);

CREATE INDEX ix_raw_quote_lookup ON raw.quote USING btree (instrument_id, observed_at DESC) INCLUDE (price, source_id);

CREATE INDEX ix_raw_quote_panel_latest ON raw.quote USING btree (instrument_id, observed_at DESC, available_at DESC) INCLUDE (price, change_pct, change_abs, source_id);

CREATE UNIQUE INDEX ux_raw_option_quote_generation_contract_observed ON raw.option_quote USING btree (capture_generation_id, contract_id, observed_at) WHERE (capture_generation_id IS NOT NULL);

CREATE UNIQUE INDEX ux_raw_fundamental_observation_period ON raw.fundamental_observation USING btree (instrument_id, source_id, metric_set, period_end, observed_at, COALESCE(period_start, '0001-01-01'::date));

CREATE UNIQUE INDEX ux_raw_option_snapshot_history_slot ON raw.option_snapshot USING btree (source_id, collection_profile, history_symbol, slot_at) WHERE (collection_profile = 'history_full'::text);
