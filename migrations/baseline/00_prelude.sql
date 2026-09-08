-- Baseline prelude: session settings, schemas, and extensions.

SET LOCAL check_function_bodies = false;

CREATE SCHEMA analysis;

CREATE SCHEMA app;

CREATE SCHEMA catalog;

CREATE SCHEMA ingest;

CREATE SCHEMA ops;

CREATE SCHEMA raw;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
