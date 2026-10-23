# Deployment

Runs on the liquidity lab's droplet (`ssh kalshi`) alongside the lab, chosen
2026-10-23 (docs/PLAN.md decision B). Everything is separate from the lab:
its own directory, venv, `.env`, database and lock file, under
`/root/kalshi_bot`. The systemd unit gives the lab priority on CPU, disk and
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
```

Data lives in `/root/kalshi_bot/data/kalshi_bot.sqlite`. It is not yet
covered by the lab's backups.
