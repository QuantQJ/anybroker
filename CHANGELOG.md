# Changelog

## 0.1.0 — 2026-11-27

- Python API for IBKR, Tradovate, Alpaca, and CCXT venues.
- Orders, brackets, cancellation, modification, positions, accounts, and quotes,
  subject to each venue's capabilities.
- Submission deduplication and tracking of unresolved writes.
- IBKR reconnection, bracket recovery, and optional submission-state persistence.
- TradingView webhook server, JSON/text alerts, shared-secret authentication,
  and SQLite journaling, available through `anyhook`.
- Tradovate bulk cancellation resolves the account before sending requests and
  filters working orders by account. Bulk cancellation and liquidation check
  broker rejection responses and stop on the first failure.
- Tradovate dependency pinned to a commit; included in `[all]` and `[dev]`.

This release is a GitHub prerelease. Tests use simulated brokers and SDK request
models; they do not establish broker acceptance or execution in paper or live
accounts. Bulk operations are sequential and cannot roll back earlier requests.
