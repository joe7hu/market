# Market PostgreSQL baseline

These SQL files are one current-state schema snapshot. They are not separate
migrations. The Alembic revision runs every `*.sql` file in filename order.

The numeric prefixes are execution order:

1. `00_prelude.sql`: session settings, schemas, and extensions.
2. `10`-`14`: analysis, application, and ingest functions.
3. `20`-`24`: tables in foreign-key dependency order.
4. `30_constraints.sql`: constraints that PostgreSQL needs to add after table creation.
5. `40`-`50`: raw functions and views.
6. `60`: indexes by schema.
7. `70`: triggers by schema.
8. `80`: protected owners.
9. `81`-`83`: schema, function, and relation privileges.

Keep `baseline_seed.sql` separate because it contains data/configuration seed,
not schema definition. Update existing databases with data-preserving maintenance
SQL first, then refresh these modules from the verified current schema.
