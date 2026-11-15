# Architecture

acme-api is a set of twelve Python services behind an API gateway. Services own their data and
communicate through HTTP APIs for queries and through events on the message bus for state changes.

- Gateway: authentication, rate limiting, request IDs.
- Services: see the service guide in AGENTS.md.
- Data: one Postgres cluster with a schema per service, Redis for caches and locks, OpenSearch for search.
- Async: Celery workers consume events; every handler is idempotent and keyed by event ID.
