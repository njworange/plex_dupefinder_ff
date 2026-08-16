# Changelog

## 1.0.0

- Initial FlaskFarm plugin release.
- Added lazy `plex_mate` connection provider without Token persistence.
- Added movie and TV episode duplicate scanning with progress and cancellation.
- Added auditable scoring, snapshot persistence, result comparison UI and action history.
- Added one-Media-at-a-time manual deletion with CSRF, one-time preview, exact confirmation, stale-state validation and post-delete verification.
- Added database compare-and-swap guards for concurrent deletion claims and per-run DELETE attempt limits.
- Added conservative recovery for process restarts during validation or deletion.
- Automatic and bulk deletion are intentionally unavailable.
