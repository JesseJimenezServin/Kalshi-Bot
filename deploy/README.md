# Deployment

Runs on the market maker's droplet (`ssh kalshi`) alongside the market maker, chosen
2026-10-23 (docs/PLAN.md decision B). Everything is separate from the market maker:
its own directory, venv, `.env`, database and lock file, under
`/root/kalshi_bot`. The systemd unit gives the market maker priority on CPU, disk and
memory, and the bot takes only 75% of the account's API rate limit.

## Deploy a commit

From the laptop, in the repo:

```bash
C=$(git rev-parse --short HEAD)
git archive --format=tar HEAD | ssh kalshi "tar -x -C /root/kalshi_bot && echo $C > /root/kalshi_bot/DEPLOYED_COMMIT"
ssh kalshi 'cd /root/kalshi_bot && ./venv/bin/pip install -q -e ".[dev]" && ./venv/bin/pytest -q && systemctl restart kalshi-bot'
```

`git archive` ships exactly the committed tree, so the droplet never runs
uncommitted code, and `DEPLOYED_COMMIT` records which commit is live.

## One-time setup (done 2026-10-23)

- Key at `/root/.kalshi/kalshi_bot_prod.pem` (chmod 600), `.env` in
  `/root/kalshi_bot` (chmod 600, never committed).
- `python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"`
- `cp deploy/kalshi-bot.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now kalshi-bot`

## Operate

```bash
ssh kalshi 'systemctl status kalshi-bot'
ssh kalshi 'journalctl -u kalshi-bot -f'                       # live JSON log
ssh kalshi 'cd /root/kalshi_bot && ./venv/bin/kalshi-bot stats' # what was recorded
ssh kalshi 'cd /root/kalshi_bot && ./venv/bin/kalshi-bot check' # auth + write guard
ssh kalshi 'cd /root/kalshi_bot && ./venv/bin/kalshi-bot census' # mispricing census (2.12)
```

Data lives in `/root/kalshi_bot/data/kalshi_bot.sqlite`.

## Backups

`kalshi-bot-backup.timer` runs daily at 04:30 UTC (an hour after the market maker's)
and uploads a VACUUM INTO snapshot, integrity-checked and gzipped, to
`offbox:kalshi-db-backups-prod/kalshi-bot/`. The script is a copy of the
market maker's. Every path it writes is the bot's own: remote folder, staging
directory and success marker
(`/var/lib/kalshi-bot-offbox/last-success`). The market maker's healthcheck reads
the market maker's marker, so this job must never touch it. Failures alert through
the market maker's ntfy topic.

```bash
ssh kalshi 'systemctl list-timers kalshi-bot-backup.timer; cat /var/lib/kalshi-bot-offbox/last-success'
ssh kalshi 'rclone lsf offbox:kalshi-db-backups-prod/kalshi-bot/'
```

Restore verified 2026-10-23: download, gunzip, `PRAGMA integrity_check` ok,
row counts matched live.
