#!/bin/bash
# Fetches the live healthy-sites pool and replaces sites.txt.
# Intended to run as a daily cron job.

set -e

API="${API:-http://127.0.0.1:8080}"
SITES_FILE="${SITES_FILE:-/home/starkz/shopi/sites.txt}"
BACKUP_DIR="/home/starkz/shopi/backups"

mkdir -p "$BACKUP_DIR"

# Backup current
if [ -f "$SITES_FILE" ]; then
    cp "$SITES_FILE" "$BACKUP_DIR/sites_$(date +%Y%m%d_%H%M%S).txt.bak"
fi

# Fetch healthy pool
HEALTHY=$(curl -sS "${API}/healthy-sites?min_score=0.15" | \
    python3 -c "import sys,json; print('\n'.join(json.load(sys.stdin)['sites']))")

COUNT=$(echo -n "$HEALTHY" | grep -c "^" || true)
echo "[*] Healthy sites: $COUNT"

if [ "$COUNT" -lt 3 ]; then
    echo "[!] Too few healthy sites ($COUNT) — not replacing sites.txt"
    exit 1
fi

# Also merge any sites already in the file but with zero samples
# (they may be new and simply not yet checked)
NEW_POOL="$HEALTHY"
if [ -f "$SITES_FILE" ]; then
    # Extract hosts already in file
    EXISTING_HOSTS=$(awk -F/ '{print $NF}' "$SITES_FILE" | sort -u)

    # Preserve any newly added sites that have no data yet
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        host=$(echo "$line" | awk -F/ '{print $NF}')
        if ! echo "$NEW_POOL" | grep -q "$host"; then
            # Check stats — keep if unknown (0 samples)
            SAMPLES=$(curl -sS "${API}/site-stats" | \
                python3 -c "import sys,json; d=json.load(sys.stdin); \
                print(d.get('$host',{}).get('samples',0))" 2>/dev/null || echo "0")
            if [ "$SAMPLES" = "0" ]; then
                NEW_POOL="${NEW_POOL}"$'\n'"${line}"
            fi
        fi
    done < "$SITES_FILE"
fi

echo "$NEW_POOL" | sort -u > "$SITES_FILE"
echo "[*] Wrote $(wc -l < "$SITES_FILE") sites to $SITES_FILE"