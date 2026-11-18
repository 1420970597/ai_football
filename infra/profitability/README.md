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

Run `execution_quality.sql` with `psql -XqAt -v ON_ERROR_STOP=1 -v anchor=<UTC event boundary>` using authorized read access to the order, signal, settlement and quote records. It emits the preceding 48-hour baseline and up to the first 48 hours after the anchor. It keeps all settled orders in the totals. The price comparison uses the preflight record for the actual execution attempt. The recommendation comparison uses matching contract fields and accepts a live basis resolved in the matching settlement record under a verified Leyu rule. The verified remaining-period rule also requires agreement between the recommendation, preflight, and submitted score baseline. Reconstructed results are diagnostic and do not count toward the real profitability target.

# Fixture exposure replay

Run `fixture_exposure.sql` with `psql -XqAt -v ON_ERROR_STOP=1 -v begin=<UTC start> -v split=<UTC cohort boundary> -v end=<UTC end> -v observed=<UTC receipt cutoff>` using read access to orders, expert signals and receipts. The replay compares no cap with a cap of one or two open fills per fixture for new automatic orders. All sports and markets are included. Manual and pre-period fills remain in the exposure. A fill releases its slot once its first settled receipt was observable. Fills with no such receipt stay open at the evidence cutoff.

Inspect coverage, tied timestamps and unresolved outcomes before interpreting the figures. The aggregate requires a single account binding and rejects mixed known currencies or incomplete fill evidence. Missing fee or currency fields are counted separately from receipt net. The counterfactual reuses existing fills and misses rejected or unsubmitted opportunities, rescheduled orders, and changes to remaining account budget. Treat the output as a concentration diagnostic.

# Source and author holdout

Run `source_holdout.sql` with the same four UTC parameters as the fixture exposure replay. It covers filled automatic orders across all markets. A source or a source and author pair qualifies for a replay exclusion if its earlier fills span at least 20 settled fixtures and three UTC placement dates and have a negative sum of net receipts per unit staked. Only settled receipts observed before the split inform that exclusion. The later cohort reports realized receipt net, settled stake, and equal-stake return by phase and in aggregate. A zero exclusion count means the training sample did not trigger the rule. The fills represent the existing policy's accepted orders, so the replay omits opportunities that the original policy did not submit or fill.

# Private wallet observations

Install `install_wallet_observation.sql` as a database administrator after confirming the production order ledger has exactly one distinct account binding and provider identity. The security-definer function exposes only that account's available balance and observation timestamp when the binding is unique and healthy. The monitor role receives function execution only, without account-table column access.

The monitor stores five-minute samples under its private runtime directory and keeps 72 hours. Each sample carries the provider observation time, balance age, and a freshness flag. Balance is fresh only when the provider observation is no more than ten minutes old and is no more than sixty seconds in the future. These samples reveal unexplained available-balance movement when compared with dated receipts. Available balance is not a wallet statement, currency evidence, or a full equity balance. No currency or fee may be inferred from the numeric amount.
