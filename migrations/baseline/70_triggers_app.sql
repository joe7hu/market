-- Triggers: app schema.

CREATE TRIGGER capture_portfolio_transaction_sector BEFORE INSERT ON app.portfolio_transaction FOR EACH ROW EXECUTE FUNCTION app.capture_portfolio_transaction_sector();

CREATE TRIGGER manual_account_snapshot_immutable BEFORE DELETE OR UPDATE ON app.manual_account_snapshot FOR EACH ROW EXECUTE FUNCTION app.prevent_manual_account_snapshot_mutation();

CREATE TRIGGER paper_execution_lineage BEFORE INSERT ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution();

CREATE TRIGGER paper_execution_observation_immutable BEFORE DELETE OR UPDATE ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER paper_order_leg_append_only BEFORE INSERT OR DELETE OR UPDATE ON app.paper_order_leg FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_leg_mutation();

CREATE CONSTRAINT TRIGGER paper_order_leg_set_complete AFTER INSERT OR UPDATE ON app.paper_order DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.require_complete_paper_order_leg_set();

CREATE TRIGGER paper_order_ticket_immutable BEFORE DELETE OR UPDATE ON app.paper_order FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_ticket_mutation();

CREATE TRIGGER phase4_paper_execution_guard BEFORE INSERT ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution_guard();
