#!/bin/bash
# Nightly database backup.
# Scheduled via cron at 11:30pm ET — after market close, before IB Gateway restart.
#
# Add to /etc/cron.d/trading-bot-backup on the VPS:
#   30 23 * * * ubuntu /opt/trading-bot/scripts/backup.sh

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_PATH="/opt/trading-bot/data/trading.db"
BACKUP_DIR="/opt/trading-bot/backups"

mkdir -p "$BACKUP_DIR"

# Use SQLite's .backup command — safe for online backups (no write lock needed)
sqlite3 "$DB_PATH" ".backup $BACKUP_DIR/trading_$TIMESTAMP.db"

echo "$(date): Backup created: trading_$TIMESTAMP.db"

# Keep last 14 days of local backups
find "$BACKUP_DIR" -name "*.db" -mtime +14 -delete

# --- Optional: upload to offsite storage via rclone ---
# Uncomment after configuring rclone with Hetzner Object Storage or S3:
#
# rclone copy "$BACKUP_DIR/trading_$TIMESTAMP.db" remote:trading-bot-backups/ \
#     --config /opt/trading-bot/.rclone.conf
#
# echo "$(date): Uploaded to remote storage"
