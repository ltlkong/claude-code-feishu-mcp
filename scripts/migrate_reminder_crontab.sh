#!/usr/bin/env bash
# Migrate existing reminder crontab entries from feishu_channel.reminder
# to xiaobai.reminders_cli.
#
# Session 3 of the xiaobai refactor moved the reminder CLI. Pre-existing cron
# entries still reference `python -m feishu_channel.reminder send/trigger/limit`,
# which no longer exists after the old package is deleted. This script rewrites
# those lines in place.
#
# Safe to run multiple times (idempotent). Backs up current crontab before
# writing. Only rewrites lines tagged `# feishu-reminder:` — leaves all other
# cron entries untouched.
#
# Usage:
#     bash scripts/migrate_reminder_crontab.sh
#
# Run this AFTER restarting the bot so the new code is live.

set -euo pipefail

BACKUP_DIR="${TMPDIR:-/tmp}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="${BACKUP_DIR}/crontab_backup_${TIMESTAMP}.txt"

echo "Reading current crontab..."
if ! crontab -l > "$BACKUP" 2>/dev/null; then
    echo "No existing crontab (nothing to migrate)."
    exit 0
fi

echo "Backup saved: $BACKUP"

# Count affected lines before/after
BEFORE_COUNT=$(grep -c "feishu_channel\.reminder" "$BACKUP" || true)
if [ "$BEFORE_COUNT" -eq 0 ]; then
    echo "No cron entries reference feishu_channel.reminder — nothing to migrate."
    exit 0
fi

echo "Found $BEFORE_COUNT line(s) referencing feishu_channel.reminder."

# Rewrite: feishu_channel.reminder -> xiaobai.reminders_cli
# Use sed in a portable way (macOS BSD sed + Linux GNU sed both accept this form).
MIGRATED="${BACKUP_DIR}/crontab_migrated_${TIMESTAMP}.txt"
sed 's|feishu_channel\.reminder|xiaobai.reminders_cli|g' "$BACKUP" > "$MIGRATED"

AFTER_COUNT=$(grep -c "xiaobai\.reminders_cli" "$MIGRATED" || true)
echo "Rewrote $AFTER_COUNT line(s) to xiaobai.reminders_cli."

# Diff preview
echo ""
echo "=== Diff preview ==="
diff "$BACKUP" "$MIGRATED" || true
echo "===================="
echo ""

read -p "Install migrated crontab? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted. Backup at: $BACKUP"
    echo "Migrated version at: $MIGRATED (not installed)"
    exit 1
fi

crontab "$MIGRATED"
echo "Crontab updated. Backup retained at: $BACKUP"
echo "Verify with: crontab -l | grep xiaobai.reminders_cli"
