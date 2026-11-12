#!/bin/bash
# COPIED VERBATIM from kalshi-market-maker/deploy/push_offbox_backup.sh
# (2026-10-23) so this bot's backups don't depend on the market maker's checkout.
# Everything bot-specific is set by kalshi-bot-backup.service through the
# environment -- in particular OFFBOX_MARKER, so this job can never refresh
# the market maker's success marker and hide a failing market-maker backup.
# Off-box backup PUSH -- runs ON THE DROPLET, uploads a compressed snapshot
# of the live database to an S3-compatible bucket.
#
# This replaces pull_droplet_backup.sh, which ran on a laptop. That design
# failed on 2026-09-30: the laptop's launchd agent could not execute the
# script (macOS TCC, repo lived in ~/Downloads) and the failure was visible
# only in a local log nobody reads, so the off-box copy silently stopped for
# two days during an unattended run. The lessons encoded here:
#   - the host that owns the data owns the backup (no second machine to fail)
#   - EVERY failure path alerts; silence must mean success, not "unknown"
#   - never let the backup itself be the thing that fills the disk
#
# Install: see deploy/README.md. Config lives in /etc/kalshi-offbox.env
# (credentials -- not in git).

set -uo pipefail

CONFIG_FILE="${KALSHI_OFFBOX_CONFIG:-/etc/kalshi-offbox.env}"
# Read the config WITHOUT clobbering anything already in the environment, so
# a one-off `DB_PATH=/nonexistent ./push_offbox_backup.sh` can override a
# single setting for testing. Plain `. "$CONFIG_FILE"` assigns
# unconditionally and silently wins over the caller -- which made the
# forced-failure check in deploy/README.md a no-op: it ran a NORMAL backup
# and exited 0 while appearing to prove the alert path (CONFIRMED 2026-10-03,
# twice, before this was fixed). A verification step that cannot fail is
# worse than none.
if [ -r "$CONFIG_FILE" ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in ''|'#'*) continue ;; esac
        _key=${_line%%=*}
        _val=${_line#*=}
        case "$_key" in [A-Za-z_][A-Za-z0-9_]*) ;; *) continue ;; esac
        eval ": \"\${$_key:=\$_val}\"" || true
        eval "export $_key"
    done < "$CONFIG_FILE"
    unset _line _key _val
fi
# NTFY_TOPIC is shared with healthcheck.sh so there is one place to watch
# and one secret to rotate.
ALERTS_ENV="${KALSHI_ALERTS_ENV:-/etc/kalshi-alerts.env}"
# shellcheck source=/dev/null
[ -z "${NTFY_TOPIC:-}" ] && [ -r "$ALERTS_ENV" ] && . "$ALERTS_ENV"

: "${DB_PATH:=/root/kalshi-market-maker/data/paper_trading.sqlite}"
: "${RCLONE_REMOTE:?RCLONE_REMOTE not set (e.g. offbox:kalshi-market-maker-backups)}"
: "${NTFY_TOPIC:?NTFY_TOPIC not set -- failure alerting is mandatory, see header}"
: "${NTFY_URL:=https://ntfy.sh}"
: "${STAGING_DIR:=/var/tmp}"
: "${KEEP_LOCAL_STAGING:=0}"

LOG_TAG="kalshi-offbox"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING_DB="${STAGING_DIR}/paper_trading_${TIMESTAMP}.sqlite"
STAGING_GZ="${STAGING_DB}.gz"

log() { echo "$(date -u +%FT%TZ) $*"; logger -t "$LOG_TAG" -- "$*" 2>/dev/null || true; }

alert() {
    # Fire-and-forget, but never let a failed alert mask the real error.
    curl -fsS --max-time 20 \
        -H "Title: Kalshi off-box backup FAILED" \
        -H "Priority: urgent" \
        -H "Tags: rotating_light" \
        -d "$1" "${NTFY_URL}/${NTFY_TOPIC}" >/dev/null 2>&1 \
        || log "WARNING: could not deliver ntfy alert"
}

cleanup() { [ "$KEEP_LOCAL_STAGING" = "1" ] || rm -f "$STAGING_DB" "$STAGING_GZ"; }

fail() {
    log "FAILED: $1"
    alert "$(printf '%s\n\nhost: %s\nsource: %s\nremote: %s' \
        "$1" "$(hostname)" "$DB_PATH" "$RCLONE_REMOTE")"
    cleanup
    exit 1
}
trap 'fail "unexpected error on line $LINENO"' ERR

log "starting push (source=$DB_PATH remote=$RCLONE_REMOTE)"

[ -r "$DB_PATH" ] || fail "source DB missing or unreadable: $DB_PATH"
command -v rclone  >/dev/null || fail "rclone not installed"
command -v sqlite3 >/dev/null || fail "sqlite3 not installed"

# Staleness check. A backup that faithfully uploads a dead database is the
# 2026-10-02 dashboard bug in backup form -- worth alerting on, because
# main.py having stopped is strictly worse news than the backup failing.
DB_AGE_S=$(( $(date +%s) - $(stat -c %Y "$DB_PATH") ))
: "${MAX_DB_AGE_S:=3600}"
if [ "$DB_AGE_S" -gt "$MAX_DB_AGE_S" ]; then
    alert "$(printf 'Source DB has not been written for %ss (limit %ss) -- main.py may be dead. Backup still attempted.\n\nhost: %s' \
        "$DB_AGE_S" "$MAX_DB_AGE_S" "$(hostname)")"
    log "WARNING: source DB is ${DB_AGE_S}s stale -- alerted, continuing"
fi

# VACUUM INTO needs free space roughly equal to the DB size, and this box is
# a 1vcpu-1gb Droplet whose disk also holds the on-box backup retention.
# Checking first turns "disk full mid-write, DB possibly wedged" into a clean
# alert. Require the DB size plus a 20% margin.
DB_BYTES=$(stat -c %s "$DB_PATH")
FREE_BLOCKS=$(stat -f --format="%a" "$STAGING_DIR")
BLOCK_SIZE=$(stat -f --format="%S" "$STAGING_DIR")
FREE_BYTES=$(( FREE_BLOCKS * BLOCK_SIZE ))
NEED_BYTES=$(( DB_BYTES + DB_BYTES / 5 ))
if [ "$FREE_BYTES" -lt "$NEED_BYTES" ]; then
    fail "insufficient free space in $STAGING_DIR: need ~${NEED_BYTES}B, have ${FREE_BYTES}B (DB is ${DB_BYTES}B)"
fi

# VACUUM INTO (not a file copy): takes a consistent snapshot of a live DB
# without blocking main.py's writers, and compacts it at the same time.
log "snapshotting ${DB_BYTES} bytes via VACUUM INTO"
sqlite3 "file:${DB_PATH}?mode=ro" "VACUUM INTO '${STAGING_DB}'" \
    || fail "VACUUM INTO failed (disk full, or DB corrupt)"

sqlite3 "file:${STAGING_DB}?mode=ro" "PRAGMA integrity_check" 2>/dev/null \
    | head -1 | grep -qx "ok" \
    || fail "snapshot failed integrity_check -- not uploading a corrupt backup"

log "compressing"
gzip -6 "$STAGING_DB" || fail "gzip failed"
GZ_BYTES=$(stat -c %s "$STAGING_GZ")

log "uploading ${GZ_BYTES} bytes to ${RCLONE_REMOTE}"
# Native b2 backend, NOT the S3-compatible endpoint. Backblaze's S3 layer
# returns 403 Forbidden on PUT for an application key that is restricted to
# a single bucket (CONFIRMED 2026-10-03: the same key authorizes fine via
# b2_authorize_account with writeFiles and uploads fine through the b2
# backend, and removing the ACL header made no difference). Since scoping
# the key to one bucket with no delete rights is the whole point, the b2
# backend is the right side of that trade.
rclone copyto "$STAGING_GZ" "${RCLONE_REMOTE}/paper_trading_${TIMESTAMP}.sqlite.gz" \
    --retries 3 --low-level-retries 10 --stats-one-line \
    || fail "rclone upload failed (credentials, network, or bucket policy)"

# Verify by reading the object back, not by trusting the exit code.
REMOTE_BYTES=$(rclone size "${RCLONE_REMOTE}/paper_trading_${TIMESTAMP}.sqlite.gz" --json 2>/dev/null \
    | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')
[ "$REMOTE_BYTES" = "$GZ_BYTES" ] \
    || fail "post-upload size mismatch: local ${GZ_BYTES}B vs remote ${REMOTE_BYTES:-missing}B"

trap - ERR
cleanup

# Success marker for healthcheck.sh. Written ONLY here, i.e. only after the
# upload and the read-back size check both passed, so its mtime is "when a
# verified off-box copy last existed". That is the one question a failure
# alert cannot answer: if this script never runs -- dead timer, masked unit,
# removed cron -- it emits no failure, and silence gets read as success.
# That was the 2026-10-02 incident, and this marker is what makes the next
# one detectable.
: "${OFFBOX_MARKER:=/var/lib/kalshi-offbox/last-success}"
mkdir -p "$(dirname "$OFFBOX_MARKER")" 2>/dev/null || true
printf '%s paper_trading_%s.sqlite.gz %s bytes (raw %s)\n' \
    "$(date -u +%FT%TZ)" "$TIMESTAMP" "$GZ_BYTES" "$DB_BYTES" > "$OFFBOX_MARKER" \
    || log "WARNING: could not write success marker $OFFBOX_MARKER"

log "OK: uploaded paper_trading_${TIMESTAMP}.sqlite.gz (${GZ_BYTES} bytes from ${DB_BYTES} bytes raw)"

# NOTE: retention is deliberately NOT done here. Use a bucket lifecycle rule
# so expiry is enforced by the storage provider even if this host is gone --
# a shell `rm` can only run while the thing it protects against is healthy.
