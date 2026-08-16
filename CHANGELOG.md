# Changelog

## 1.1.0

- Added opt-in batch-approved semi-automatic deletion for safe groups with exactly two active Media versions and one unique score winner.
- Added persisted batch plans, one-time nonce approval, exact confirmation, a manual/batch shared global DB lease, sequential execution, cancellation, progress and conservative restart recovery.
- Added cross-group Part path collision detection and stop-on-first-error behavior for batch deletion.
- Added direct per-group deletion entry from result rows.
- Added library select-all and clear-all controls.
- Added batch settings, status UI, audit-safe serialization and regression tests.

## 1.0.0

- Initial FlaskFarm plugin release.
- Added lazy `plex_mate` connection provider without Token persistence.
- Added movie and TV episode duplicate scanning with progress and cancellation.
- Added auditable scoring, snapshot persistence, result comparison UI and action history.
- Added one-Media-at-a-time manual deletion with CSRF, one-time preview, exact confirmation, stale-state validation and post-delete verification.
- Added database compare-and-swap guards for concurrent deletion claims and per-run DELETE attempt limits.
- Added conservative recovery for process restarts during validation or deletion.
- Automatic and bulk deletion were intentionally unavailable in this release.
