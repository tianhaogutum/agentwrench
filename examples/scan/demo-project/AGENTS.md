# AGENTS.md - acme-api

This file is the single source of truth for coding agents working in this repository.
Read it fully before making changes. Sections are ordered by how often they matter.

## Repository layout

- `services/<name>/` - one deployable service per directory (see the service guide below).
- `libs/` - shared libraries: `acme_db`, `acme_http`, `acme_events`, `acme_testing`.
- `migrations/` - Alembic migrations, one linear history for the shared Postgres cluster.
- `deploy/` - Helm charts and Terraform modules. Never edit generated files under `deploy/generated/`.
- `tools/` - developer scripts. Run them through `make`, not directly.

## Everyday commands

- `make setup` installs the toolchain (uv, pre-commit hooks, local Postgres and Redis containers).
- `make test` runs the unit tests for all services; `make test SERVICE=orders` runs one service.
- `make lint` runs ruff, mypy (strict for `libs/`) and the import-boundary checker.
- `make e2e` starts the docker-compose stack and runs the end-to-end suite; it takes about 12 minutes.
- `make fmt` formats the code. CI fails on unformatted code, so run it before committing.

## Coding conventions

- Python 3.12, type hints everywhere, `from __future__ import annotations` at the top of every module.
- Public functions have docstrings that explain *why*, not *what*. Keep modules under 500 lines.
- No new runtime dependencies without an ADR in `docs/adr/`. Prefer the standard library.
- Money is always an integer number of minor units plus an ISO 4217 currency code. Never use floats.
- Timestamps are timezone-aware UTC `datetime` objects. Convert at the edges only.
- Log with `structlog`; include `request_id`, `tenant_id` and the entity ID in every log line.
- Raise domain errors from `libs/acme_errors`; HTTP handlers map them to problem+json responses.

## Testing rules

- Every bug fix comes with a regression test that fails before the fix.
- Unit tests must not touch the network or the real database; use the fakes in `acme_testing`.
- Integration tests are marked `@pytest.mark.integration` and run against the compose stack.
- Keep test data builders in `tests/builders.py` of each service; do not inline large fixtures.
- Snapshot tests live next to the code they cover and are updated with `make snapshots`.

## Service guide

### accounts

- Stack: FastAPI + SQLAlchemy. Entry point: `services/accounts/app.py`; settings in `services/accounts/settings.py`.
- Owns the `accounts` schema. Other services read it only through the `accounts` HTTP API or its events.
- Events published: `accounts.created`, `accounts.updated`, `accounts.deleted` (see `libs/acme_events/schemas/accounts.json`).
- Run locally with `make run SERVICE=accounts`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for accounts live in `services/accounts/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the accounts worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/accounts/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of accounts, update `services/accounts/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `accounts_` prefix. Dashboards
  are generated from `deploy/dashboards/accounts.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/accounts/settings.py` and can be overridden with an
  `ACME_ACCOUNTS_` environment variable. Document new settings in `services/accounts/README.md`.
- Testing notes: `make test SERVICE=accounts` runs in about a minute. Integration tests for accounts need the compose
  stack (`make up`) and use the `accounts_test` database, which is recreated on every run.
- Runbook: alerts for accounts page the owning team listed in `CODEOWNERS`. Before touching accounts in an incident,
  check the `#accounts-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/accounts/` need a review from the accounts owners; cross-service changes also need
  a platform reviewer.

### billing

- Stack: FastAPI + Stripe adapter. Entry point: `services/billing/app.py`; settings in `services/billing/settings.py`.
- Owns the `billing` schema. Other services read it only through the `billing` HTTP API or its events.
- Events published: `billing.created`, `billing.updated`, `billing.deleted` (see `libs/acme_events/schemas/billing.json`).
- Run locally with `make run SERVICE=billing`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for billing live in `services/billing/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the billing worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/billing/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of billing, update `services/billing/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `billing_` prefix. Dashboards
  are generated from `deploy/dashboards/billing.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/billing/settings.py` and can be overridden with an
  `ACME_BILLING_` environment variable. Document new settings in `services/billing/README.md`.
- Testing notes: `make test SERVICE=billing` runs in about a minute. Integration tests for billing need the compose
  stack (`make up`) and use the `billing_test` database, which is recreated on every run.
- Runbook: alerts for billing page the owning team listed in `CODEOWNERS`. Before touching billing in an incident,
  check the `#billing-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/billing/` need a review from the billing owners; cross-service changes also need
  a platform reviewer.

### catalog

- Stack: FastAPI + OpenSearch. Entry point: `services/catalog/app.py`; settings in `services/catalog/settings.py`.
- Owns the `catalog` schema. Other services read it only through the `catalog` HTTP API or its events.
- Events published: `catalog.created`, `catalog.updated`, `catalog.deleted` (see `libs/acme_events/schemas/catalog.json`).
- Run locally with `make run SERVICE=catalog`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for catalog live in `services/catalog/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the catalog worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/catalog/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of catalog, update `services/catalog/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `catalog_` prefix. Dashboards
  are generated from `deploy/dashboards/catalog.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/catalog/settings.py` and can be overridden with an
  `ACME_CATALOG_` environment variable. Document new settings in `services/catalog/README.md`.
- Testing notes: `make test SERVICE=catalog` runs in about a minute. Integration tests for catalog need the compose
  stack (`make up`) and use the `catalog_test` database, which is recreated on every run.
- Runbook: alerts for catalog page the owning team listed in `CODEOWNERS`. Before touching catalog in an incident,
  check the `#catalog-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/catalog/` need a review from the catalog owners; cross-service changes also need
  a platform reviewer.

### checkout

- Stack: FastAPI. Entry point: `services/checkout/app.py`; settings in `services/checkout/settings.py`.
- Owns the `checkout` schema. Other services read it only through the `checkout` HTTP API or its events.
- Events published: `checkout.created`, `checkout.updated`, `checkout.deleted` (see `libs/acme_events/schemas/checkout.json`).
- Run locally with `make run SERVICE=checkout`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for checkout live in `services/checkout/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the checkout worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/checkout/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of checkout, update `services/checkout/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `checkout_` prefix. Dashboards
  are generated from `deploy/dashboards/checkout.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/checkout/settings.py` and can be overridden with an
  `ACME_CHECKOUT_` environment variable. Document new settings in `services/checkout/README.md`.
- Testing notes: `make test SERVICE=checkout` runs in about a minute. Integration tests for checkout need the compose
  stack (`make up`) and use the `checkout_test` database, which is recreated on every run.
- Runbook: alerts for checkout page the owning team listed in `CODEOWNERS`. Before touching checkout in an incident,
  check the `#checkout-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/checkout/` need a review from the checkout owners; cross-service changes also need
  a platform reviewer.

### inventory

- Stack: Celery workers. Entry point: `services/inventory/app.py`; settings in `services/inventory/settings.py`.
- Owns the `inventory` schema. Other services read it only through the `inventory` HTTP API or its events.
- Events published: `inventory.created`, `inventory.updated`, `inventory.deleted` (see `libs/acme_events/schemas/inventory.json`).
- Run locally with `make run SERVICE=inventory`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for inventory live in `services/inventory/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the inventory worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/inventory/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of inventory, update `services/inventory/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `inventory_` prefix. Dashboards
  are generated from `deploy/dashboards/inventory.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/inventory/settings.py` and can be overridden with an
  `ACME_INVENTORY_` environment variable. Document new settings in `services/inventory/README.md`.
- Testing notes: `make test SERVICE=inventory` runs in about a minute. Integration tests for inventory need the compose
  stack (`make up`) and use the `inventory_test` database, which is recreated on every run.
- Runbook: alerts for inventory page the owning team listed in `CODEOWNERS`. Before touching inventory in an incident,
  check the `#inventory-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/inventory/` need a review from the inventory owners; cross-service changes also need
  a platform reviewer.

### notifications

- Stack: Celery + SES adapter. Entry point: `services/notifications/app.py`; settings in `services/notifications/settings.py`.
- Owns the `notifications` schema. Other services read it only through the `notifications` HTTP API or its events.
- Events published: `notifications.created`, `notifications.updated`, `notifications.deleted` (see `libs/acme_events/schemas/notifications.json`).
- Run locally with `make run SERVICE=notifications`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for notifications live in `services/notifications/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the notifications worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/notifications/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of notifications, update `services/notifications/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `notifications_` prefix. Dashboards
  are generated from `deploy/dashboards/notifications.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/notifications/settings.py` and can be overridden with an
  `ACME_NOTIFICATIONS_` environment variable. Document new settings in `services/notifications/README.md`.
- Testing notes: `make test SERVICE=notifications` runs in about a minute. Integration tests for notifications need the compose
  stack (`make up`) and use the `notifications_test` database, which is recreated on every run.
- Runbook: alerts for notifications page the owning team listed in `CODEOWNERS`. Before touching notifications in an incident,
  check the `#notifications-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/notifications/` need a review from the notifications owners; cross-service changes also need
  a platform reviewer.

### orders

- Stack: FastAPI + SQLAlchemy. Entry point: `services/orders/app.py`; settings in `services/orders/settings.py`.
- Owns the `orders` schema. Other services read it only through the `orders` HTTP API or its events.
- Events published: `orders.created`, `orders.updated`, `orders.deleted` (see `libs/acme_events/schemas/orders.json`).
- Run locally with `make run SERVICE=orders`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for orders live in `services/orders/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the orders worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/orders/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of orders, update `services/orders/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `orders_` prefix. Dashboards
  are generated from `deploy/dashboards/orders.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/orders/settings.py` and can be overridden with an
  `ACME_ORDERS_` environment variable. Document new settings in `services/orders/README.md`.
- Testing notes: `make test SERVICE=orders` runs in about a minute. Integration tests for orders need the compose
  stack (`make up`) and use the `orders_test` database, which is recreated on every run.
- Runbook: alerts for orders page the owning team listed in `CODEOWNERS`. Before touching orders in an incident,
  check the `#orders-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/orders/` need a review from the orders owners; cross-service changes also need
  a platform reviewer.

### payments

- Stack: FastAPI + ledger library. Entry point: `services/payments/app.py`; settings in `services/payments/settings.py`.
- Owns the `payments` schema. Other services read it only through the `payments` HTTP API or its events.
- Events published: `payments.created`, `payments.updated`, `payments.deleted` (see `libs/acme_events/schemas/payments.json`).
- Run locally with `make run SERVICE=payments`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for payments live in `services/payments/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the payments worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/payments/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of payments, update `services/payments/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `payments_` prefix. Dashboards
  are generated from `deploy/dashboards/payments.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/payments/settings.py` and can be overridden with an
  `ACME_PAYMENTS_` environment variable. Document new settings in `services/payments/README.md`.
- Testing notes: `make test SERVICE=payments` runs in about a minute. Integration tests for payments need the compose
  stack (`make up`) and use the `payments_test` database, which is recreated on every run.
- Runbook: alerts for payments page the owning team listed in `CODEOWNERS`. Before touching payments in an incident,
  check the `#payments-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/payments/` need a review from the payments owners; cross-service changes also need
  a platform reviewer.

### pricing

- Stack: pure Python library. Entry point: `services/pricing/app.py`; settings in `services/pricing/settings.py`.
- Owns the `pricing` schema. Other services read it only through the `pricing` HTTP API or its events.
- Events published: `pricing.created`, `pricing.updated`, `pricing.deleted` (see `libs/acme_events/schemas/pricing.json`).
- Run locally with `make run SERVICE=pricing`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for pricing live in `services/pricing/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the pricing worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/pricing/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of pricing, update `services/pricing/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `pricing_` prefix. Dashboards
  are generated from `deploy/dashboards/pricing.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/pricing/settings.py` and can be overridden with an
  `ACME_PRICING_` environment variable. Document new settings in `services/pricing/README.md`.
- Testing notes: `make test SERVICE=pricing` runs in about a minute. Integration tests for pricing need the compose
  stack (`make up`) and use the `pricing_test` database, which is recreated on every run.
- Runbook: alerts for pricing page the owning team listed in `CODEOWNERS`. Before touching pricing in an incident,
  check the `#pricing-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/pricing/` need a review from the pricing owners; cross-service changes also need
  a platform reviewer.

### search

- Stack: OpenSearch indexer. Entry point: `services/search/app.py`; settings in `services/search/settings.py`.
- Owns the `search` schema. Other services read it only through the `search` HTTP API or its events.
- Events published: `search.created`, `search.updated`, `search.deleted` (see `libs/acme_events/schemas/search.json`).
- Run locally with `make run SERVICE=search`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for search live in `services/search/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the search worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/search/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of search, update `services/search/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `search_` prefix. Dashboards
  are generated from `deploy/dashboards/search.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/search/settings.py` and can be overridden with an
  `ACME_SEARCH_` environment variable. Document new settings in `services/search/README.md`.
- Testing notes: `make test SERVICE=search` runs in about a minute. Integration tests for search need the compose
  stack (`make up`) and use the `search_test` database, which is recreated on every run.
- Runbook: alerts for search page the owning team listed in `CODEOWNERS`. Before touching search in an incident,
  check the `#search-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/search/` need a review from the search owners; cross-service changes also need
  a platform reviewer.

### shipping

- Stack: FastAPI + carrier adapters. Entry point: `services/shipping/app.py`; settings in `services/shipping/settings.py`.
- Owns the `shipping` schema. Other services read it only through the `shipping` HTTP API or its events.
- Events published: `shipping.created`, `shipping.updated`, `shipping.deleted` (see `libs/acme_events/schemas/shipping.json`).
- Run locally with `make run SERVICE=shipping`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for shipping live in `services/shipping/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the shipping worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/shipping/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of shipping, update `services/shipping/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `shipping_` prefix. Dashboards
  are generated from `deploy/dashboards/shipping.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/shipping/settings.py` and can be overridden with an
  `ACME_SHIPPING_` environment variable. Document new settings in `services/shipping/README.md`.
- Testing notes: `make test SERVICE=shipping` runs in about a minute. Integration tests for shipping need the compose
  stack (`make up`) and use the `shipping_test` database, which is recreated on every run.
- Runbook: alerts for shipping page the owning team listed in `CODEOWNERS`. Before touching shipping in an incident,
  check the `#shipping-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/shipping/` need a review from the shipping owners; cross-service changes also need
  a platform reviewer.

### webhooks

- Stack: aiohttp receiver. Entry point: `services/webhooks/app.py`; settings in `services/webhooks/settings.py`.
- Owns the `webhooks` schema. Other services read it only through the `webhooks` HTTP API or its events.
- Events published: `webhooks.created`, `webhooks.updated`, `webhooks.deleted` (see `libs/acme_events/schemas/webhooks.json`).
- Run locally with `make run SERVICE=webhooks`; the API is served on the port listed in `deploy/ports.yaml`.
- Feature flags for webhooks live in `services/webhooks/flags.py`. Remove a flag within two releases after rollout.
- Known pitfalls: the webhooks worker retries idempotent jobs up to five times; make every handler idempotent and
  key it by the event ID. Background jobs must finish within 30 seconds or be split into smaller jobs.
- Database access goes through repositories in `services/webhooks/repo.py`. Do not issue raw SQL from handlers.
- When you change the public API of webhooks, update `services/webhooks/openapi.yaml` and run `make contracts` so
  that consumer contract tests in the other services pick up the change.
- Metrics: every endpoint exports latency histograms and error counters with the `webhooks_` prefix. Dashboards
  are generated from `deploy/dashboards/webhooks.jsonnet`; regenerate them with `make dashboards`.
- Configuration: every setting has a default in `services/webhooks/settings.py` and can be overridden with an
  `ACME_WEBHOOKS_` environment variable. Document new settings in `services/webhooks/README.md`.
- Testing notes: `make test SERVICE=webhooks` runs in about a minute. Integration tests for webhooks need the compose
  stack (`make up`) and use the `webhooks_test` database, which is recreated on every run.
- Runbook: alerts for webhooks page the owning team listed in `CODEOWNERS`. Before touching webhooks in an incident,
  check the `#webhooks-oncall` channel and the latest deploy in the release dashboard.
- Ownership: changes to `services/webhooks/` need a review from the webhooks owners; cross-service changes also need
  a platform reviewer.

## Database migrations

- Create a migration with `make migration NAME=short_description`; review the generated SQL by hand.
- Migrations must be backwards compatible with the previous release: add columns as nullable, backfill in a
  separate job, then add constraints in a later release.
- Never rename or drop a column in the same release that stops using it.
- Large backfills run as batched jobs in `tools/backfills/`, never inside an Alembic migration.

## API design

- Resource names are plural nouns; identifiers are opaque prefixed strings such as `ord_8f3k2`.
- Pagination uses cursors (`?cursor=...&limit=...`); never expose offsets.
- Errors follow RFC 9457 problem+json with a stable `type` URI per error.
- Every write endpoint accepts an `Idempotency-Key` header and stores the response for 24 hours.

## Code review expectations

- Reviews focus on correctness, data safety and operability before style; style is enforced by tooling.
- Keep pull requests under 400 changed lines where possible; split mechanical refactors from behavior changes.
- Every pull request description explains the user-visible effect, the rollout plan and how it was tested.
- Reviewers run the change locally when it touches migrations, money handling or authentication.
- Resolve every review thread explicitly; do not merge with unresolved threads or failing required checks.

## Local environment troubleshooting

- `make doctor` checks tool versions, container health and that the local database accepts connections.
- If Postgres fails to start, remove the `acme-pg` volume with `make reset-db`; this deletes local data only.
- Port conflicts usually come from an old compose stack: run `make down` before `make up`.
- Slow tests are usually caused by a missing `--reuse-db`; the default `make test` target already sets it.
- If imports fail after switching branches, run `make setup` again to resync the virtual environment.

## Glossary
- **tenant**: a customer organization; every row carries a tenant_id.
- **SKU**: a sellable catalog item.
- **ledger**: the append-only record of money movements in payments.
- **hold**: an authorized but uncaptured payment.
- **reservation**: inventory set aside for an open checkout.
- **fulfilment**: picking, packing and shipping an order.
- **backfill**: a batched job that rewrites existing rows.
- **contract test**: a consumer-driven API test.
- **dark launch**: code deployed behind a flag that is off.
- **canary**: the first 5% of pods that get a release.

## Error catalog

Every service maps domain errors to problem+json types. Reuse an existing type before adding one.

### accounts errors

- `https://errors.acme.dev/accounts/not-found` (404): the referenced accounts entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/accounts/conflict` (409): the accounts entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/accounts/invalid-state` (422): the requested accounts transition is not allowed from the current state.
- `https://errors.acme.dev/accounts/rate-limited` (429): the tenant exceeded the accounts write quota; back off using Retry-After.

### billing errors

- `https://errors.acme.dev/billing/not-found` (404): the referenced billing entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/billing/conflict` (409): the billing entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/billing/invalid-state` (422): the requested billing transition is not allowed from the current state.
- `https://errors.acme.dev/billing/rate-limited` (429): the tenant exceeded the billing write quota; back off using Retry-After.

### catalog errors

- `https://errors.acme.dev/catalog/not-found` (404): the referenced catalog entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/catalog/conflict` (409): the catalog entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/catalog/invalid-state` (422): the requested catalog transition is not allowed from the current state.
- `https://errors.acme.dev/catalog/rate-limited` (429): the tenant exceeded the catalog write quota; back off using Retry-After.

### checkout errors

- `https://errors.acme.dev/checkout/not-found` (404): the referenced checkout entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/checkout/conflict` (409): the checkout entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/checkout/invalid-state` (422): the requested checkout transition is not allowed from the current state.
- `https://errors.acme.dev/checkout/rate-limited` (429): the tenant exceeded the checkout write quota; back off using Retry-After.

### inventory errors

- `https://errors.acme.dev/inventory/not-found` (404): the referenced inventory entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/inventory/conflict` (409): the inventory entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/inventory/invalid-state` (422): the requested inventory transition is not allowed from the current state.
- `https://errors.acme.dev/inventory/rate-limited` (429): the tenant exceeded the inventory write quota; back off using Retry-After.

### notifications errors

- `https://errors.acme.dev/notifications/not-found` (404): the referenced notifications entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/notifications/conflict` (409): the notifications entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/notifications/invalid-state` (422): the requested notifications transition is not allowed from the current state.
- `https://errors.acme.dev/notifications/rate-limited` (429): the tenant exceeded the notifications write quota; back off using Retry-After.

### orders errors

- `https://errors.acme.dev/orders/not-found` (404): the referenced orders entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/orders/conflict` (409): the orders entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/orders/invalid-state` (422): the requested orders transition is not allowed from the current state.
- `https://errors.acme.dev/orders/rate-limited` (429): the tenant exceeded the orders write quota; back off using Retry-After.

### payments errors

- `https://errors.acme.dev/payments/not-found` (404): the referenced payments entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/payments/conflict` (409): the payments entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/payments/invalid-state` (422): the requested payments transition is not allowed from the current state.
- `https://errors.acme.dev/payments/rate-limited` (429): the tenant exceeded the payments write quota; back off using Retry-After.

### pricing errors

- `https://errors.acme.dev/pricing/not-found` (404): the referenced pricing entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/pricing/conflict` (409): the pricing entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/pricing/invalid-state` (422): the requested pricing transition is not allowed from the current state.
- `https://errors.acme.dev/pricing/rate-limited` (429): the tenant exceeded the pricing write quota; back off using Retry-After.

### search errors

- `https://errors.acme.dev/search/not-found` (404): the referenced search entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/search/conflict` (409): the search entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/search/invalid-state` (422): the requested search transition is not allowed from the current state.
- `https://errors.acme.dev/search/rate-limited` (429): the tenant exceeded the search write quota; back off using Retry-After.

### shipping errors

- `https://errors.acme.dev/shipping/not-found` (404): the referenced shipping entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/shipping/conflict` (409): the shipping entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/shipping/invalid-state` (422): the requested shipping transition is not allowed from the current state.
- `https://errors.acme.dev/shipping/rate-limited` (429): the tenant exceeded the shipping write quota; back off using Retry-After.

### webhooks errors

- `https://errors.acme.dev/webhooks/not-found` (404): the referenced webhooks entity does not exist or belongs to another tenant.
- `https://errors.acme.dev/webhooks/conflict` (409): the webhooks entity was modified concurrently; retry with the latest version.
- `https://errors.acme.dev/webhooks/invalid-state` (422): the requested webhooks transition is not allowed from the current state.
- `https://errors.acme.dev/webhooks/rate-limited` (429): the tenant exceeded the webhooks write quota; back off using Retry-After.

## Security and secrets

- Never commit secrets. Local secrets live in `.env.local`, which is git-ignored; CI uses the vault.
- Never log tokens, card numbers or full email addresses. Use `acme_logging.redact()` for user data.
- Customer data must not leave the EU region; do not add third-party services without a DPA review.
- Treat every webhook payload as untrusted input: verify signatures before parsing.

## Release process

- Releases are cut from `main` every Tuesday by the release bot. Do not tag releases by hand.
- A change that needs a coordinated deploy must be behind a feature flag and listed in `RELEASE_NOTES.md`.
- Hotfixes branch from the latest release tag and are merged back into `main` the same day.

## Incident runbook

- If production alerts fire while you work, stop and tell the user; do not attempt fixes on your own.
- Rollbacks use `make rollback SERVICE=<name> VERSION=<tag>` and require a second approver.
- Write the incident timeline in `docs/incidents/` within 24 hours.
