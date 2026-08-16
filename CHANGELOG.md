# Changelog

## 1.2.0

- Added an opt-in post-delete Plex partial scan mode: disabled by default, Plex Media Scanner (Binary), or Plex Web API.
- Added non-destructive Plex connection and plex_mate Binary-helper diagnostics plus fail-closed setting validation.
- Added a one-hour global operational quarantine with automatic retry disabled when a Binary scanner child cannot be proven terminated.
- Added a durable best-effort scan outbox with bounded retry after confirmed deletion; DELETE requests are never retried by this workflow.
- Added fail-closed preflight target resolution: an enabled post-delete scan must resolve an exact movie folder or TV-show root before DELETE starts.
- Added a read-only history panel for recent post-delete scan status, target, attempts and sanitized result/error details.
- Documented movie-folder and TV-show-root targeting, runtime requirements and the separation between Plex partial scans and DupeFinder duplicate rescans.

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
