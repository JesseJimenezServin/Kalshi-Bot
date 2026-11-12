#!/bin/bash
# Healthcheck for kalshi-bot, run from cron every 5 minutes. Alerts through
# the same ntfy topic as the market maker's /root/healthcheck.sh (topic in
# /etc/kalshi-alerts.env, chmod 600, never in git). Checks:
#
#   1. kalshi-bot is active
#   2. it is ALIVE: a heartbeat in the journal within the last 5 min (a hung
#      but "active" process looks fine to systemctl)
#   3. recording is not paused by the disk guard, and free disk is above a
#      warning level set ABOVE the guard's floor, so you hear before it trips
#   4. new markets are not being dropped at the watch cap (skipped_capacity
#      grew since the last run)
#   5. books are not going stale en masse (WS feed silently broken)
#   6. an off-box backup has SUCCEEDED in the last 36 h
#
# Like the market maker's check, it cannot report that the whole droplet is gone.

set -u

ALERTS_ENV="${KALSHI_ALERTS_ENV:-/etc/kalshi-alerts.env}"
_env_topic="${NTFY_TOPIC:-}"
# shellcheck source=/dev/null
[ -r "$ALERTS_ENV" ] && . "$ALERTS_ENV"
[ -n "$_env_topic" ] && NTFY_TOPIC="$_env_topic"
if [ -z "${NTFY_TOPIC:-}" ]; then
    echo "$(date -u +%FT%TZ) FATAL: NTFY_TOPIC unset (looked in $ALERTS_ENV) -- cannot alert" >&2
    exit 2
fi

UNIT=kalshi-bot
DATA_DIR="${DATA_DIR:-/root/kalshi_bot/data}"
DISK_WARN_GB="${DISK_WARN_GB:-6}"          # the recorder pauses at 4 GB (config.yaml)
STALE_SHARE_WARN="${STALE_SHARE_WARN:-0.5}"
OFFBOX_MARKER="${OFFBOX_MARKER:-/var/lib/kalshi-bot-offbox/last-success}"
OFFBOX_STALE_SECONDS=129600
STATE_DIR="${STATE_DIR:-/var/lib/kalshi-bot-healthcheck}"
NOTIFY_THROTTLE_SECONDS=3600

mkdir -p "$STATE_DIR" 2>/dev/null || true

# notify <key> <priority> <message>: throttled per key, first alert immediate.
notify() {
    _key="$1"; _prio="$2"; _msg="$3"
    _stamp="${STATE_DIR}/${_key}.last"
    _now=$(date +%s)
    if [ -f "$_stamp" ]; then
        _last=$(cat "$_stamp" 2>/dev/null || echo 0)
        case "$_last" in ''|*[!0-9]*) _last=0 ;; esac
        if [ $((_now - _last)) -lt "$NOTIFY_THROTTLE_SECONDS" ]; then
            echo "$(date -u +%FT%TZ) [$_key] suppressed (throttled): $_msg" >&2
            return 0
        fi
    fi
    if curl -s -m 10 -H "Priority: ${_prio}" -H "Title: kalshi-bot: ${_key}" \
            -d "$_msg" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1; then
        echo "$_now" > "$_stamp"
        echo "$(date -u +%FT%TZ) [$_key] ALERT: $_msg" >&2
    else
        echo "$(date -u +%FT%TZ) WARNING: could not deliver alert [$_key]: $_msg" >&2
    fi
}
clear_issue() { rm -f "${STATE_DIR}/${1}.last" 2>/dev/null || true; }

HOST=$(hostname)

# 1. active
if ! systemctl is-active --quiet "$UNIT"; then
    notify bot-down urgent "$UNIT is NOT active on ${HOST} ($(systemctl is-active "$UNIT" 2>&1)). RECORDING HAS STOPPED."
    exit 0   # the remaining checks read a live process
fi
clear_issue bot-down

# 2. alive: last heartbeat (logged every 60 s) within 5 min
HB=$(journalctl -u "$UNIT" --since "-5min" --no-pager -o cat 2>/dev/null | grep '"event": "heartbeat"' | tail -1)
if [ -z "$HB" ]; then
    notify bot-stuck urgent "$UNIT on ${HOST} is active but has logged no heartbeat in 5 min (hung?)"
    exit 0
fi
clear_issue bot-stuck

# Pull the fields the remaining checks need out of the heartbeat JSON.
read -r PAUSED WATCHING STALE SKIPPED < <(printf '%s' "$HB" | python3 -c '
import json, sys
h = json.load(sys.stdin)
print(int(h["recorder"].get("paused_low_disk", False)), h.get("watching", 0), h.get("stale_books", 0),
      h.get("skipped_capacity", 0))' 2>/dev/null || echo "x x x x")
if [ "$PAUSED" = "x" ]; then
    notify heartbeat-unparsed default "could not parse $UNIT heartbeat on ${HOST}; the healthcheck needs updating"
    exit 0
fi
clear_issue heartbeat-unparsed

# 3. disk
FREE_GB=$(df -P -BG "$DATA_DIR" | awk 'NR==2 {gsub("G","",$4); print $4}')
if [ "$PAUSED" = "1" ]; then
    notify recording-paused urgent "$UNIT on ${HOST}: recording PAUSED by the disk guard (${FREE_GB} GB free)"
else
    clear_issue recording-paused
fi
if [ "${FREE_GB:-0}" -lt "$DISK_WARN_GB" ]; then
    notify disk-low high "${HOST}: only ${FREE_GB} GB free (recording pauses at 4 GB). DB: $(du -sh "$DATA_DIR" | cut -f1)"
else
    clear_issue disk-low
fi

# 4. watch cap: skipped_capacity is a counter since process start; alert when it grows
PREV_FILE="${STATE_DIR}/skipped_capacity"
if [ -f "$PREV_FILE" ]; then
    PREV=$(cat "$PREV_FILE" 2>/dev/null || echo 0)
    case "$PREV" in ''|*[!0-9]*) PREV=0 ;; esac
    if [ "$SKIPPED" -ge "$PREV" ]; then DELTA=$((SKIPPED - PREV)); else DELTA=$SKIPPED; fi  # restart resets it
else
    DELTA=0  # first run: no baseline
fi
echo "$SKIPPED" > "$PREV_FILE"
if [ "$DELTA" -gt 0 ]; then
    notify watch-cap default "$UNIT on ${HOST}: ${DELTA} new markets NOT recorded since the last check (watch cap full, watching ${WATCHING})"
fi

# 5. stale books
if [ "$WATCHING" -gt 20 ] && python3 -c "import sys; sys.exit(0 if $STALE / $WATCHING > $STALE_SHARE_WARN else 1)"; then
    notify books-stale high "$UNIT on ${HOST}: ${STALE} of ${WATCHING} watched books are stale (WS feed broken?)"
else
    clear_issue books-stale
fi

# 6. off-box backup recency
if [ -f "$OFFBOX_MARKER" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$OFFBOX_MARKER") ))
    if [ "$AGE" -gt "$OFFBOX_STALE_SECONDS" ]; then
        notify backup-stale high "${HOST}: last successful kalshi-bot off-box backup was $((AGE / 3600)) h ago"
    else
        clear_issue backup-stale
    fi
else
    notify backup-stale high "${HOST}: no kalshi-bot off-box backup marker at ${OFFBOX_MARKER}"
fi
exit 0
