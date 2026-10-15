#!/usr/bin/env python3
"""
Prune dead sites from sites.txt using the API's real-time /site-stats
and /quarantine endpoints.

Usage:
  python3 prune_sites.py                    # dry run, prints what would be removed
  python3 prune_sites.py --apply            # actually rewrite sites.txt
  python3 prune_sites.py --api http://127.0.0.1:8080 --apply
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from urllib.parse import urlparse

API = "http://127.0.0.1:8080"
SITES_FILE = "/home/starkz/shopi/sites.txt"
BACKUP_DIR = "/home/starkz/shopi/backups"

MAX_SUCCESS_RATE = 10.0        # remove if success < 10%
MIN_SAMPLES = 8                # only if we have at least this many samples
KEEP_UNKNOWN = True            # keep sites we haven't sampled yet


def _fetch(path: str) -> dict:
    url = f"{API}{path}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm_host(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    return urlparse(url).netloc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API)
    ap.add_argument("--sites-file", default=SITES_FILE)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-success", type=float, default=MAX_SUCCESS_RATE)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    args = ap.parse_args()

    global API
    API = args.api

    if not os.path.exists(args.sites_file):
        print(f"[ERR] {args.sites_file} not found", file=sys.stderr)
        return 1

    print(f"[*] Fetching stats from {API}")
    try:
        stats = _fetch("/site-stats")
        quarantine = _fetch("/quarantine?min_hits=3")
    except Exception as ex:
        print(f"[ERR] API unreachable: {ex}", file=sys.stderr)
        return 1

    with open(args.sites_file, "r", encoding="utf-8", errors="ignore") as f:
        sites = [ln.strip() for ln in f if ln.strip()]

    print(f"[*] Sites in file: {len(sites)}")

    to_remove: dict[str, str] = {}

    # Rule 1: quarantine list
    for host, info in quarantine.items():
        to_remove[host] = f"quarantined ({info['hits']}x {info['last_response']})"

    # Rule 2: low success rate
    for host, info in stats.items():
        if info.get("blacklisted"):
            to_remove.setdefault(host, "blacklisted")
            continue
        if info.get("has_products") is False:
            to_remove.setdefault(host, "no products")
            continue
        samples = info.get("samples", 0)
        rate_str = info.get("success_rate", "N/A")
        if samples >= args.min_samples and rate_str != "N/A":
            try:
                rate = float(rate_str.rstrip("%"))
            except ValueError:
                continue
            if rate < args.max_success:
                to_remove.setdefault(host, f"low success ({rate:.1f}% / {samples} samples)")

    if not to_remove:
        print("[*] Nothing to remove. All sites are healthy (or not sampled).")
        return 0

    print(f"\n[*] Removing {len(to_remove)} hosts:")
    for h, reason in sorted(to_remove.items()):
        print(f"     - {h:45s}  {reason}")

    kept = [s for s in sites if _norm_host(s) not in to_remove]
    print(f"\n[*] Before: {len(sites)}  After: {len(kept)}  Removed: {len(sites) - len(kept)}")

    if not args.apply:
        print("\n[DRY RUN] Rerun with --apply to modify sites.txt")
        return 0

    # Backup
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(
        BACKUP_DIR,
        f"sites_{time.strftime('%Y%m%d_%H%M%S')}.txt.bak",
    )
    shutil.copy2(args.sites_file, backup_path)
    print(f"[*] Backup saved: {backup_path}")

    with open(args.sites_file, "w", encoding="utf-8") as f:
        for s in kept:
            f.write(s + "\n")
    print(f"[*] Wrote {len(kept)} sites to {args.sites_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())