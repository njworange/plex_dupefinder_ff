# Changelog

## 1.6.0

- Reworked the results workflow around scan/score, one-click selected-version deletion, and an opt-in automatic cleanup started by the user; neither deletion path asks for typed confirmation or a browser confirmation dialog, while server-side CSRF, one-time nonce, fresh plan digest and global lease checks remain mandatory.
- Automatic cleanup now retains the single unique highest-score Media in every eligible group and processes every lower-score Media without a plan-size cap. Highest-score ties, unsafe groups (including multipart under the default policy), cross-group shared paths, ambiguous file/subtitle plans and quarantine groups with 3+ active Media are excluded with reasons.
- Changed automatic execution isolation so a failed item skips only the remaining items in its group and independent eligible groups continue. Direct mode can safely finalize multiple intentional deletions from a 3+ Media group against their combined expected survivor set.
- Added explicit DB-only force cleanup for terminal scan results that are otherwise retained by `critical`/`recovery_required` journals; files, Plex items, journals and protection/recovery data remain untouched, while live workers and valid leases still block cleanup.
- Added per-row DB force-delete controls for ActionLog and post-delete scan history. Hidden rows become scrubbed `history_deleted` tombstones so numeric IDs are not reused; connected recovery journals remain intact and no file/PMS operation is invoked.
- Preserved scrubbed `scan_run`, `duplicate_group`, and `media_candidate` tombstones during scan-result cleanup so SQLite cannot reuse any referenced audit ID; normal result APIs hide all three tombstone types.
- Persisted every automatic-cleanup exclusion in a dedicated `batch_exclusion` audit table, including all-excluded reviews and cross-group subtitle/path conflicts, so reasons remain visible after refresh or reconnect without entering the worker queue.
- Added independent pagination for post-delete scan history so every terminal Job remains reachable for DB-only force deletion instead of disappearing beyond the previous 100-row window.

## 1.5.1

- Changed hybrid direct-delete partial scans to target the user-selected surviving Media folder (or TV show root) instead of the deleted candidate folder, so a normally removed empty candidate directory no longer produces a false terminal `blocked` result.
- Kept the scan target narrow and fail-closed: direct deletion requires exactly one retained target inside the current Plex Location and allowed roots, never widens to a section/library root, and still blocks if the retained target disappears before the worker runs.
- Preserved fixed, credential-free `PostDeleteScanBlocked` reasons in job history so missing retained folders, scanner configuration, section identity and path-policy failures are distinguishable.

## 1.5.0

- Changed the persisted `direct` mode from FlaskFarm-side video unlinking to one PMS Media DELETE followed by separate cleanup of target-exclusive same-stem external subtitles, eliminating the mergerfs video-rename `EXDEV` path.
- Added durable full SHA-256 protection copies for survivor-owned, shared and ambiguous regular sidecars before PMS DELETE, with verified no-overwrite restoration when PMS removes protected subtitles as collateral.
- Blocked the PMS DELETE before mutation when a related protected sidecar cannot be snapshotted safely; target-exclusive subtitles are removed only after an exact PMS post-read confirms the target Media is gone and every retained Media fingerprint is unchanged.
- Kept direct-mode protection copies in FlaskFarm `path_data` until the mandatory Binary/Web partial scan and retained-version verification complete; uncertain operations keep their journal and protection data for manual review without retrying DELETE.
- Replaced direct-mode exact confirmations with `DELETE MEDIA ...` / `BATCH DELETE MEDIA ...`; stale `DELETE FILES` batch previews cannot be approved and must be regenerated.
- Added terminal Recent Scans result deletion for `duplicate_group` and `media_candidate` rows while retaining a scrubbed, API-hidden `results_deleted` scan tombstone to prevent Run ID reuse; files, Plex items and all deletion/audit work records remain preserved, and active linked work blocks deletion.

## 1.4.1

- Reworked direct deletion to use a randomized same-parent handoff path, avoiding mergerfs cross-branch child-directory renames while keeping every mutation journaled before it starts.
- Added open-file-descriptor and content proof for mergerfs path-hash inode modes, followed by an immediate path identity recheck before unlinking; this protects normal concurrent replacement without claiming an adversarial zero-race guarantee.
- Treated explicitly unsupported FUSE directory `fsync` errors as audited best-effort durability limitations while keeping other sync failures fatal.
- Added mutation-aware failure classification and stage/exception/errno/journal diagnostics: pre-mutation failures remain blocked for a fresh reviewed retry, while post-mutation uncertainty remains `recovery_required` for manual review.
- Added read-only state diagnostics for legacy `recovery_required` direct-delete journals; legacy source and handoff paths are never automatically deleted or restored.
- Removed the extra browser confirmation dialog for individual deletion while retaining the server-enforced exact confirmation phrase, one-time nonce, plan digest and CSRF checks.

## 1.4.0

- Removed the per-scan deletion-attempt cap while retaining the atomic database attempt counter, scan-status guard, global deletion lease and audit history.
- Removed the legacy attempt-limit setting from the UI; an already-persisted `setting_max_delete_per_run` value is ignored and not migrated or rewritten.
- Added an explicitly selected direct-filesystem backend that permanently deletes one validated video and only external subtitles proven exclusive to it, without requiring a quarantine root.
- Kept ambiguous, shared, linked and survivor-owned subtitle files out of the direct-delete plan and exposed every excluded path and reason for review.
- Required an exact digest-bound confirmation plus Binary/Web partial scan and retained-version verification for direct deletion.

## 1.3.1

- Added a visible per-scan deletion-attempt budget with used, maximum and remaining counts in scan and result views.
- Disabled individual preview and batch-plan controls when the budget is exhausted, while retaining authoritative server-side and atomic database guards.
- Added actionable guidance that an explicitly raised live limit applies to the current scan without changing existing saved settings or requiring a new scan.
- Kept the conservative default limit of one attempt and now rejects an exhausted request before Plex or filesystem preflight work begins.

## 1.3.0

- Added an opt-in quarantine backend that never calls Plex Media DELETE and moves one validated video version to a configured same-filesystem quarantine root.
- Added conservative sidecar subtitle discovery for common Plex subtitle names; only subtitles proven exclusive to the removed video version are quarantined with it.
- Added fail-closed protection for retained and ambiguous/shared subtitles, with full paths and exclusion reasons in preview, results and durable audit details.
- Added subtitle exception filtering in History and dedicated escaped UI rendering for quarantined and excluded subtitle paths.
- Added mandatory Binary/Web partial-scan preflight for quarantine mode so PMS state and retained versions can be revalidated after the move.
- Kept the existing Plex backend as the compatibility default; safe subtitle cleanup remains explicitly opt-in and quarantine files are never automatically purged.

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
