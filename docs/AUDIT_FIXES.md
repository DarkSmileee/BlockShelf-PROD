# Audit fixes and safe rollout

This file records the original audit fixes and subsequent fixes below. Nothing
has been deployed to the server. Tests use isolated databases and temporary
storage; the copied production data is unchanged. Apply all migrations before
serving this release: 0004 changes location uniqueness and 0005 adds atomic
rate-limit counters and page-size validators.

## Changes

| Finding | Change |
| --- | --- |
| 1 | Invitations require the intended account. GET displays a confirmation; acceptance and revocation require CSRF-protected POST. Acceptance locks its invitation record. |
| 2 | `/login/` and `/accounts/login/` use the same protected password-login handler. Social login URLs remain available. |
| 3 | Post-login destinations must belong to the current host and use a safe scheme. |
| 4 | Client IP comes from the connection unless its immediate peer is explicitly trusted; only that peer's `X-Real-IP` is accepted. GET requests do not consume login attempts. |
| 5 | New backups live outside public media. Legacy backups remain available through authorized downloads until `relocate_backups` moves their files. Relative database paths stay unchanged. |
| 6 | Updates require an explicit revision, a clean tracked checkout, a successful database dump, and a checked code/configuration archive. Dependencies come from the tested lock file. Failed updates stop instead of claiming success. |
| 7 | Restore requires a new destination and accepts either PostgreSQL URL scheme and raw or gzip backups. PostgreSQL archives are checked before creation, then restored in one transaction. The live database is never dropped. |
| 8 | Backup filenames include random unique identifiers; files are published only after successful writing. Failed writes leave no published backup record. SQLite uses its online backup API. |
| 9 | Imports reject invalid nonempty quantities with a row/field error, and roll back the import. Whole-number decimals such as `12.0` are accepted; thousands separators are rejected. |
| 10 | Import/export honor the selected inventory owner and existing view/edit permissions. |
| 11 | Failed catalog batches return an error and their unchanged offset after rollback. |
| 12 | Maintenance reloads and briefly locks each item after network calls, then saves only still-missing name/image fields. Validation and audit signals remain active. |
| 13 | Maintenance yields between items after its 15-second work budget. Each external lookup checks the same deadline/rate budget, including BrickLink and suffix fallbacks. An in-flight request may finish after the budget, subject to its existing timeout. |
| 14 | Tests use separate in-memory settings, temporary storage and mocked external services. Broken names, fields and fixtures were corrected; the legacy inventory tests are also collected. |
| 15 | SMTP host, port, credentials, TLS/SSL and timeout are read from the environment. |
| 16 | Scheduled backup commands return failure if any requested backup fails. User retention honors `--keep`; zero retention is rejected. |
| 17 | Refreshing a public share revokes prior active links and creates a new expiry/access state. |
| 18 | Archives have an expanded-size bound, gzip expansion is bounded, and inventory rows are limited while reading. Catalog temporary files are removed after completion/preparation failure or after abandonment. |
| 19 | The inventory-switcher route reuses the existing inventory page and selector. |
| 20 | The unused upload-security module and its python-magic dependency were removed. Historical security documents are marked accordingly; antivirus scanning is not implemented. |

## Verification of the earlier UI fixes (historical)

- 121 tests passed, including the existing user flows and new regression tests.
- Application coverage: 72.25%, above the existing 70% requirement.
- Django system checks passed; migration drift check reported no changes.
- Recovery shell scripts were syntax-checked with Bash. Updater failure tests ran
  with fake commands; no system services or PostgreSQL databases were changed.
- Three existing Django deprecation warnings remain; Django is locked below 6.

## Test locally

Install `requirements.dev.txt` into a separate virtual environment and run
`python -m pytest` from the application directory. Pytest selects
`blockshelf_inventory.test_settings`; it does not load the server `.env`.
The default command retains the original 70% coverage requirement.

The recovery tests use temporary SQLite files and mocked PostgreSQL commands.
The updater tests use fake commands and temporary files, never system services.
On Windows, Git Bash may need permission to run outside the process sandbox.

The runtime lock file records the versions exercised locally. Verify the locked
dependencies on the server's Linux/Python version before choosing a release.
The original copied server environment and its installed versions were absent.

## Before deployment

1. Preserve the server's current code, environment configuration, media and a fresh
   PostgreSQL dump outside the server. Verify a restore into a separate database.
2. Test this release on Linux staging using that separate database, isolated API
   credentials and an SMTP test service. Check login, Google login, inventory
   edits/import/export, collaboration, public shares and backup downloads.
3. Inspect the **active** Nginx configuration under `/etc`. Do not copy the bundled
   example wholesale: it still requires your domain and actual certificate paths.
   Add this restriction inside the active server block, validate with `nginx -t`,
   then reload Nginx:

   ```nginx
   location ^~ /media/backups/ { return 404; }
   ```

4. Make the immediate reverse proxy overwrite `X-Real-IP` with its verified client
   address. Set `TRUSTED_PROXY_IPS` to the immediate proxy addresses only (default:
   `127.0.0.1,::1`). Match the active Nginx upstream and Gunicorn bind to
   `127.0.0.1:8000`. Existing Unix-socket or public bind overrides must be changed
   as part of the same staged cutover; the updater does not rewrite custom units.
   Permit the Bootstrap CDN and Cloudflare Turnstile sources as shown in the
   bundled CSP if applying that policy. If Cloudflare is in front, configure Nginx's trusted real-IP
   handling first; do not trust arbitrary forwarded headers from the Internet.
5. Set `BACKUP_ROOT` if needed. Default: `/opt/blockshelf/private_backups`; it must
   be outside `MEDIA_ROOT`. Ensure the service account can write it and that Nginx
   does not serve it. `REBRICKABLE_TEMP_ROOT` defaults to private_imports beside it.
6. Choose a tested Git revision containing these changes. Run the revised updater
   with that revision: `sudo bash /path/to/tested-release/scripts/update.sh TESTED_COMMIT_SHA`.
   Use the revised script, not an older copy already on the server. It stops the
   application and scheduled backup writers before the final snapshot. Stop any
   separately launched imports or maintenance jobs too.
   It refuses uncommitted tracked changes and requires the existing `.env` link.
   It records the old revision, dependency list, database dump and installation
   archive in a private `/var/backups/blockshelf-update-*` directory.
7. The updater applies migrations and runs `redact_audit_secrets` and
   `relocate_backups`. For a manual deployment, run `python manage.py migrate`,
   `python manage.py redact_audit_secrets`, and `python manage.py relocate_backups`
   as the service account after blocking public
   backup URLs. The command moves every legacy file, refuses conflicting targets,
   and can resume after partial progress without changing database paths.
8. The updater installs the revised backup service and reloads systemd. It
   preserves an existing timer file/schedule and installs the timer only if missing.
   For manual deployment, do that explicitly. Cleanup removes abandoned imports
   and expired rate-limit counters. The updater restores the timer only when it
   was active before the update; a failure leaves the writers stopped.
9. Verify service readiness, authenticated backup download and unauthenticated
   backup denial. Perform another backup/restore check after deployment.

Do not transfer the local virtual environment, test coverage output, copied
production logs or backups as part of a source release.

## Restore and rollback

The restore command no longer overwrites a destination:

```bash
bash scripts/restore_database.sh /path/database.sql postgres --target blockshelf_recovery
bash scripts/restore_database.sh /path/database.sql.gz postgres --target blockshelf_recovery
bash scripts/restore_database.sh /path/database.sqlite3 sqlite --target /private/new-recovery.sqlite3
```

PostgreSQL requires a maintenance credential with permission to create the new
database. The ordinary installed application role does not have that permission;
the command will fail safely rather than dropping anything. If restoring as a
different role, check ownership and application access before any cutover.
Supply the maintenance connection through `DATABASE_URL` without placing its
password in command history. Existing environment values override `.env` values.

Check inventory counts, accounts, collaboration, constraints and sample records
in the restored database before changing the service connection. Preserve the
old database until the replacement is accepted. A failed restore may leave an
empty new database; investigate it separately, never delete the live database.

If an update fails, the script prints the recovery directory and old revision.
Keep the service and backup timer stopped until code, installed dependencies and database state
are compatible. Restore the recorded code/configuration and dependency versions;
if a database rollback is required, restore its saved dump into a new database
and validate before cutover. Do not blindly roll code back across migrations.

## Limits

Local checks do not establish the live proxy configuration, PostgreSQL role
permissions, multi-process PostgreSQL locking, real email delivery, external API
availability, Linux service behavior or full-load performance. Those need staging.

The two additional UI defects are now fixed. The theme endpoint saves the same
browser cookie used by the navigation toggle, without a nonexistent database field
or a schema change. The empty theme settings field was replaced by guidance to the
existing toggle. Cookie preferences take precedence over older local storage;
system mode follows the device appearance, including when local storage is blocked.
Public-share headers use the existing direction helper and the view's `avail`/`loc`
parameters. Sort clicks preserve search, reset pagination, and toggle direction.
Regression checks cover rendered links, query results, cookie validation/CSRF,
and theme JavaScript in a simulated browser environment. No full browser visual
inspection or server deployment has been performed.


## Location-aware inventory and save feedback (2026-11-11)

Inventory entries are now unique by owner, part ID, colour, and storage location.
For example, yellow 3003 at 1562 and yellow 3003 at 1563 are separate entries.
An exact duplicate at the same location is rejected visibly, with submitted form
values preserved. Success and error messages are rendered in the shared layout.
CSV/Excel imports match all four fields; an omitted location means the blank
location, not a match against an arbitrary existing location. Existing rows and
quantities are not rewritten by the migration.

Deploy migration `0004_remove_inventoryitem_unique_user_part_color_and_more`
using the normal Django migrate step before enabling writes with the new code.
Take a database backup first. Once multiple locations exist for the same part
and colour, the old uniqueness constraint cannot be restored without resolving
those entries; do not blindly reverse this migration or run the older importer.

Validation: 129 tests passed, 72.32% coverage, Django system check and migration
drift check passed. An isolated populated SQLite migration preserved the original
row and accepted a second location. Production PostgreSQL and browser visual
checks have not been run. Live search is not included in these two fixes.


## Live inventory search (2026-11-11)

Inventory and public shared inventory search after a 300 ms pause in typing.
The previous browser filter only examined the current page; the new script
requests the existing permission-checked server view and updates the table and
pagination together. It preserves owner and sorting, resets the page, updates
the URL, and keeps the search input focused. Clearing the query restores the
inventory. Old responses are ignored immediately when typing resumes, even
before the next request starts. Loading, no-results and failure messages are
announced; failures leave the previous results visible with an explicit warning.
Enter and Search remain normal GET navigation, including when JavaScript is off.

No new dependency, endpoint, or database migration is needed for live search.
Include static/js/inventory-search.js in the normal collectstatic deployment step.
The earlier location migration is still required for the location changes.
Validation: JavaScript behavior checks cover debounce, stale responses, owner
mismatch, failures/redirects, clearing, IME composition and submission. Django
checks cover matches beyond the initial page and read-only inventory scope on
both authenticated and public views. Real browser visual and production-load
checks have not been performed. No deployment was performed.


## Second full audit: 16 fixes

| Finding | Resolution |
| --- | --- |
| 1 | Inventory edit forms submit the original updated_at version. Saving locks and compares the current record; stale edits return a visible conflict with submitted values preserved. Missing versions are rejected. |
| 2 | Maintenance checks the current part number after locking, before applying lookup data. |
| 3 | /admin/login/ uses the protected login handler and retains the staff gate. |
| 4 | Password login completes through allauth, including mandatory email verification. |
| 5 | The first AppConfig record takes its registration default from the environment; explicit existing administrator choices remain authoritative. |
| 6 | Supplied Gunicorn/service/Nginx defaults consistently use loopback TCP. Active server overrides need the coordinated rollout described above. |
| 7 | UserPreference audit logging excludes the API key. The idempotent redact_audit_secrets command removes old key fields while preserving other audit changes; the updater invokes it. |
| 8 | Database conditional UPDATE statements reserve rate-limit attempts atomically across workers, including external lookup limits. Expired counters are cleaned by the scheduled service. |
| 9 | The updater stops application and scheduled backup writers before the final dump/archive. Failure keeps writers stopped. |
| 10 | Fresh installation creates the application directory with service-user ownership before cloning. |
| 11 | Both inventory searches resolve exact element IDs through the catalogue, matching the part and colour together. Misleading hidden-element storage UI was removed. |
| 12 | Rate-limited maintenance preserves its cursor and returns retry_after. The browser waits and retries the same item rather than reporting a false miss. |
| 13 | Page-size validators enforce 5–500 server-side; private/public views clamp legacy preferences. |
| 14 | The example CSP permits required Bootstrap and Turnstile scripts/styles/frames/connections. |
| 15 | Legacy backup dispatcher counters no longer trigger set -e; both engines and cleanup are exercised. Missing sqlite3 fails instead of copying a live database. |
| 16 | Installation and update instructions use a tested revision, requirements.lock, and required migrations. |

Historic offline database backups may still contain old audit-key copies. The
redaction command operates on the selected live/staging database only; it does
not rewrite backup archives or revoke external API keys. Retain protected backups
according to your recovery policy and rotate exposed keys if necessary.

Do not deploy until Linux staging verifies the active proxy configuration,
PostgreSQL migration and rollback/recovery, real browser flows, and email delivery.


### Final local verification for the second audit fixes

- 158 tests passed; none skipped. Application coverage: 73.43% (requirement: 70%).
- Django system checks and migration-drift checks passed.
- Migration 0005 applied to a populated isolated SQLite database, preserving both
  3003/Yellow locations (1562 and 1563) and their quantities.
- Parallel database connections exercised the atomic attempt limit.
- Bash tests exercised failed dump/archive/migration/start stages, both present
  and absent backup units, plus both legacy backup branches and cleanup. All
  deployment shell scripts passed syntax checks. Commands and services were fake.
- Browser-script tests exercised maintenance retry timing and cursor retention;
  existing live-search and theme tests still pass.
- Three pre-existing Django deprecation warnings remain. Production PostgreSQL,
  Linux services, Nginx, browser visual checks and external delivery remain staging
  checks; no live service or production data was changed.


### Eight follow-up audit findings

| Finding | Local fix and regression coverage |
|---|---|
| 1. Email recovery mismatch | Profile changes only edit the username. The account page links to allauth email management with ACCOUNT_CHANGE_EMAIL enabled. Tests submit a replacement, verify that the old address remains current while confirmation is pending, confirm the replacement, and check that the old address no longer resolves for recovery. |
| 2. Import overwrites concurrent edits | Existing inventory rows use select_for_update inside the import transaction. Tests check that the read requests a lock and omitted quantities survive. A separate PostgreSQL test interleaves a quantity edit with an import and verifies the import waits and preserves the committed quantity. |
| 3. Reimport truncates notes | Already-contained notes are left intact; only new notes are appended. Repeated import tests cover contained, equal, new and empty text. |
| 4. Share access race | One conditional database update checks active status, token, expiration and remaining accesses while incrementing the count. Stale-reader tests verify only one access is reserved and revoked, rotated or expired links cannot reserve access. |
| 5. BOM catalogue headers | Both web and command-line catalogue readers use utf-8-sig. Tests cover web batches and plain/gzip command-line input. |
| 6. Missing/revoked share errors | Http404 is handled with the existing unavailable-share page and HTTP 404. Tests verify inventory content is not disclosed. |
| 7. Repeated catalogue parsing | Preparation records file positions every 100 logical CSV records. Each batch seeks to a checkpoint and skips at most 99 records. Immutable checkpoints support retries without advancing past a failed transaction. Tests cover BOMs, quoted multiline records, late offsets, rollback, retry and subsequent batches. |
| 8. Unused forms | Removed unused PasswordChangeForm and InventoryImportForm, their import, and template references to nonexistent import options. Tests exercise the actual password and upload forms. |

Verification: 176 passed, 1 skipped, 74.89% application coverage (70% required).
Django system checks and migration-drift checks passed. No new schema migration
is required for these eight fixes. The three existing deprecation warnings remain.
The previous profile test now verifies that an arbitrary posted email cannot bypass
verification; the new end-to-end test exercises the replacement email flow instead.

The PostgreSQL concurrency regression is included but skipped under SQLite. This
machine has no usable PostgreSQL server binaries. Run that test against a dedicated
PostgreSQL test database before deployment. Real email delivery and Linux/browser
staging checks remain outstanding. Nothing was deployed or changed on the server.


## Production rollout completed

The earlier no-deployment statements describe historical checkpoints. See
[the November 11 deployment record](operations/DEPLOYMENT_2026-11-11.md) for
production verification, the final message fix, configuration changes and follow-ups.
