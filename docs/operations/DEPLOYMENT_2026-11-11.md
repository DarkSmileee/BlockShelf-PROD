# Production deployment record: 2026-11-11

The audit fixes, inventory-location support, live search, and follow-up save-message
fix were deployed through WinSCP. The application source in this commit includes
the final save-message fix confirmed in the browser. This repository records source
and operational history; it is not a database or credentials backup.

## Verified

- Linux/Python 3.12 staging suite on PostgreSQL: 177 passed, none skipped before
  the final template-only message fix, including concurrent import protection.
- After the message fix: 178 passed, one PostgreSQL-only test skipped in the
  isolated SQLite run. That concurrency test had passed in the PostgreSQL run.
- A production database dump restored into a separate PostgreSQL instance.
  Migrations 0004 and 0005 preserved every inventory row byte-for-byte in the
  checked representation (2,244 rows, total quantity 13,832 at rehearsal).
- Cutover checked the inventory fingerprint again before serving the new code.
- Readiness and login page responded successfully. Public /media/backups/ URLs
  returned 404. Five historical audit records had API secrets redacted, and legacy
  backups moved into private storage.
- The user confirmed both locations 1562 and 1563 for part 3003/Yellow, live
  search, editing isolation, permissions/sharing, backup download, and finally
  visible save confirmations.

## Production configuration changes (secrets are not stored here)

- Gunicorn binds to 127.0.0.1:8000; Nginx proxies to the same address.
- Nginx sets X-Real-IP from its remote address; trusted application proxies are
  127.0.0.1 and ::1. Any additional upstream proxy requires its own verified real-IP
  configuration; forwarding headers must not be trusted from arbitrary clients.
- Nginx blocks /media/backups/; BACKUP_ROOT is /opt/blockshelf/private_backups,
  and REBRICKABLE_TEMP_ROOT is /opt/blockshelf/private_imports.
- The existing application systemd service and backup timer schedule were kept.
  The backup service gained temporary-import and rate-limit cleanup commands.
- The production database role owned its tables but lacked CREATE on schema
  public. The first migration attempt rolled back; inventory fingerprints matched.
  CREATE was then granted to role blockshelf in database blockshelf, and deployment
  resumed successfully. Include schema privilege and table ownership checks in
  future deployment preflights; an isolated database owner does not test them.

## Recovery and future updates

Fresh database and installation backups were taken with writers stopped. The
installation archive includes the old virtualenv and configuration. Keep those
private recovery files off-server too. They are intentionally excluded from Git.

The adjacent deployment script is a HISTORICAL one-time script, pinned to that
upload archive and staging folder. It is not the normal updater and should not be
rerun on the deployed installation. Its original migration permission failure was
resolved separately as described above. The later message patch changed only
base.html and inventory_list.html after staging tests and a template backup.

The server still has its original Git metadata plus file-based deployed changes.
Pushing this repository does not synchronize the server's checkout. Do not run
blind git pull/reset commands there. Preserve server-only changes and establish a
clean, tested release before using scripts/update.sh, which requires a clean tracked
checkout and an explicit tested revision.

## Deferred or outstanding

- Email sending was explicitly deferred: the console email backend does not
  deliver password-reset or email-change confirmation messages.
- A new post-deployment full-backup restore check remains outstanding. The earlier
  production-backup restore and migration rehearsal passed.
- Logs showed an old cached Django model version warning and systemd notification
  warnings. These were not fixed as part of the template patch.
- A kernel update was pending; a planned reboot remains separate maintenance.
