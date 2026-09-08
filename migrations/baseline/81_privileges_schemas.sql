-- Privileges: schemas.

GRANT USAGE ON SCHEMA analysis TO market_research_signer;

GRANT USAGE ON SCHEMA analysis TO market_app;

GRANT USAGE ON SCHEMA app TO market_app;

GRANT USAGE ON SCHEMA catalog TO market_research_signer;

GRANT USAGE ON SCHEMA catalog TO market_app;

GRANT USAGE ON SCHEMA ingest TO market_app;

GRANT USAGE ON SCHEMA ingest TO market_migrator;

GRANT USAGE ON SCHEMA ops TO market_app;

GRANT USAGE ON SCHEMA public TO market_research_signer;

GRANT USAGE ON SCHEMA raw TO market_app;
