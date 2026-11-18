# Settlement history check

Run `reconcile_upstream.py` in the executor environment with scoped, authenticated input on stdin. It queries settled order history for four adjacent 12-hour UTC windows. The input contract is:

- `start`: timezone-aware first boundary, at least 48 hours in the past.
- `scope`, `base_url`, `request_id`, `origin`: the authorized account session accepted by `VenueSession`.
- `local`: order rows for that same account and binding. Each row requires `order_no`, `stake`, `payout`, `net_profit`, `settled_at`, and `booked_at`. Use the provider's settlement time as `booked_at` when present and the receipt observation time otherwise. Include earlier placements that settled in the audited window.

Supply the input through a private process pipe. The result contains counts, window boundaries, and error categories. It omits order numbers, account identifiers, credentials, and monetary amounts.

The check compares reported stake, payout, and profit with decimal arithmetic. `complete_page` requires the provider's stated total to match the returned valid settled orders. A pagination limit, a mismatching or unknown time, or absent costs remains visible in the result. The current executor exposes absent or unmapped costs as null. Separate wallet evidence is required to establish the currency, fee treatment, and account-level net result.

# Continuous snapshots

The monitor stores the first observed snapshot after each window closes under `state/window-closes/<ordinal>.json`. It also saves the first snapshot at or after the original 48-hour deadline as `state/window-closes/initial-48h.json`. These files survive the routine five-minute checkpoint rotation. A timestamp gap can skip per-window files during downtime. The current snapshot continues to reflect late receipts, and the preserved deadline file records what the monitor first observed after the deadline.

For each settled order the monitor compares the projection to the latest stored settled receipt. The window includes aggregate counts of missing and inconsistent receipts. Four-window verification requires the same platform and provider account, one known currency, and verifiable costs. Apply `grants.sql` when updating the dedicated monitor role. Snapshot output contains neither order identifiers nor account identities.

# Execution price diagnostic

Run `execution_quality.sql` with `psql -XqAt -v ON_ERROR_STOP=1 -v anchor=<UTC event boundary>` using authorized read access to the order, signal and quote records. It emits the preceding 48-hour baseline and up to the first 48 hours after the anchor. It keeps all settled orders in the totals. The price comparison uses the preflight record for the actual execution attempt. The separate recommendation comparison requires matching settlement rules and excludes live orders whose full-period basis is unverified. A reconstructed result is diagnostic and does not count toward the real profitability target.
