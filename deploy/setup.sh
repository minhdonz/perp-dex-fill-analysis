#!/usr/bin/env bash
# One-command VPS bootstrap (fresh Ubuntu 22.04/24.04, sudo-capable user):
#
#   curl -fsSL https://raw.githubusercontent.com/minhdonz/perp-dex-fill-analysis/main/deploy/setup.sh | bash
#
# Optional: export HEALTHCHECK_URL=https://hc-ping.com/<uuid> first to get
# email/push alerts when a feed goes silent (free account at healthchecks.io).
#
# Installs deps, clones the repo, sets up the venv, installs + starts the
# systemd collector service, and adds cron entries for the nightly rollup and
# the 5-minute healthcheck. Idempotent: safe to re-run to update.
set -euo pipefail

# Repo is private while pre-launch (PRD P2: open-source deferred) — on the
# VPS, override with the deploy-key SSH remote:
#   REPO=git@github.com:minhdonz/perp-dex-fill-analysis.git
REPO="${REPO:-https://github.com/minhdonz/perp-dex-fill-analysis.git}"
DIR="$HOME/perp-dex-fill-analysis"

sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv git sqlite3

if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only
else
    git clone "$REPO" "$DIR"
fi
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# systemd unit, with this user and clone path substituted in
sed -e "s|^User=.*|User=$USER|" \
    -e "s|/home/minh/perp-dex-fill-analysis|$DIR|g" \
    deploy/collector.service | sudo tee /etc/systemd/system/collector.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now collector
sudo systemctl restart collector   # pick up new code on re-runs

# cron: nightly rollup + healthcheck every 5 min (replaces prior entries)
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v 'perp-dex-fill-analysis' > "$TMP" || true
{
    cat "$TMP"
    echo "15 0 * * * cd $DIR && .venv/bin/python -m analysis.rollup --db data/fills.db >> rollup.log 2>&1  # perp-dex-fill-analysis"
    echo "*/5 * * * * cd $DIR && .venv/bin/python scripts/healthcheck.py --db data/fills.db${HEALTHCHECK_URL:+ --ping $HEALTHCHECK_URL} >> healthcheck.log 2>&1  # perp-dex-fill-analysis"
    echo "30 2 * * * cd $DIR && .venv/bin/python scripts/fetch_hl_fees.py data/fills.db --top 500 >> fees.log 2>&1  # perp-dex-fill-analysis"
} | crontab -
rm -f "$TMP"

echo "--- waiting 10s for first writes..."
sleep 10
systemctl is-active collector
sqlite3 data/fills.db "SELECT venue, COUNT(*) AS trades FROM trades GROUP BY 1" || true
echo "--- done. watch with: journalctl -u collector -f"
