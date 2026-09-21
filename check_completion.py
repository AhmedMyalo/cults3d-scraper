"""Report coverage against the site's OWN reported totals.

Never compare against a hard-coded target: the catalogue grows while the crawl
runs. Two independent reference points are used -
  1. the URL index built from Cults3D's sitemaps (what we set out to fetch), and
  2. the per-category totals Cults3D prints in each listing page <title>
     (e.g. "177k Best 3D printing files of Kitchen"),
so a systematic gap shows up as a disagreement between them rather than
silently looking complete.

Some listings vanish permanently between indexing and fetching, so allow a
small ABSOLUTE residue rather than a percentage.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# At ~3.15M models, a few thousand permanently-deleted designs is normal
# attrition, not a bug in the crawl.
ALLOWED_RESIDUE = 5_000


def load_index_slugs(index_dir):
    slugs = set()
    for p in sorted(glob.glob(os.path.join(index_dir, "urls_*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    slugs.add(json.loads(line)["slug"])
                except (ValueError, KeyError):
                    continue
    return slugs


def load_scraped(out_dir):
    rows = {}
    for p in glob.glob(os.path.join(out_dir, "part_*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("slug"):
                    rows[r["slug"]] = r      # dedupe by slug across shards
    return rows


def site_category_totals(cats):
    """Scrape each category's own reported total out of its listing <title>."""
    from cults_session import get, make_session
    s = make_session()
    out = {}
    for c in cats:
        try:
            html = get(s, f"https://cults3d.com/en/categories/{c}").text
            m = re.search(r"<title>\s*[^\d<]*([\d.,]+\s*[kKmM]?)\b", html)
            out[c] = m.group(1).strip() if m else None
        except Exception as e:
            out[c] = f"error: {type(e).__name__}"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="url_index")
    ap.add_argument("--out", default="details")
    ap.add_argument("--check-site", action="store_true",
                    help="also fetch each category's reported total (slow)")
    args = ap.parse_args()

    indexed = load_index_slugs(args.index)
    scraped = load_scraped(args.out)
    missing = len(indexed - set(scraped))

    print(f"indexed (from sitemaps) : {len(indexed):>10,}")
    print(f"scraped (unique)        : {len(scraped):>10,}")
    print(f"missing                 : {missing:>10,}")
    if indexed:
        print(f"coverage                : {100*len(scraped)/len(indexed):>9.2f}%")

    groups = Counter(r.get("group") for r in scraped.values())
    print("\nby top-level group:")
    for g, n in groups.most_common():
        print(f"  {str(g):<16} {n:>9,}")

    # Data-quality signals that a silent parser regression would show up in.
    if scraped:
        vals = list(scraped.values())
        print("\nfield fill rates:")
        for f in ("views", "downloads", "likes", "makes", "comments",
                  "collections", "rating_value", "price", "author"):
            n = sum(1 for r in vals if r.get(f) is not None)
            flag = "  <-- LOW" if n / len(vals) < 0.80 else ""
            print(f"  {f:<14} {100*n/len(vals):>6.1f}%{flag}")

    if args.check_site:
        cats = sorted({c for r in scraped.values()
                       for c in (r.get("categories") or [])})
        print(f"\nsite-reported totals for {len(cats)} categories:")
        for c, t in site_category_totals(cats).items():
            print(f"  {c:<28} {t}")

    complete = missing <= ALLOWED_RESIDUE and len(indexed) > 0
    print(f"\nCOMPLETE={complete} (residue allowance {ALLOWED_RESIDUE:,})")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
