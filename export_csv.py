"""Export the JSONL shards to CSV.

Dedupes by design id across shards, because a model can be reached from more
than one place and parallel workers are only disjoint by URL, not by identity.
List fields are joined with "|" rather than left as Python repr so the CSV is
usable in Excel and pandas without re-parsing.

Splits the output at a row cap: a single 3.15M-row CSV is both unwieldy and
larger than GitHub's 100MB file limit.
"""
import argparse
import csv
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

COLUMNS = [
    "design_id", "slug", "url", "title", "author", "author_url",
    "group", "categories", "tags",
    # Engagement first - these are the point of the dataset.
    "views", "views_raw", "views_is_approx",
    "downloads", "likes", "makes", "comments", "collections",
    "rating_value", "rating_count",
    "price", "currency", "is_free",
    "file_count", "file_format", "file_names",
    "license", "is_no_ai", "usages",
    # The designer's own totals, as shown on the model page. Like views,
    # these are rendered rounded ("1k designs", "13.1k downloads").
    "author_designs", "author_downloads", "author_followers",
    "author_sales", "author_sales_currency",
    "author_is_certified", "author_seller_badge",
    "license_url", "published_at",
    "description", "printing_settings", "is_auto_translated",
    "image", "scraped_at",
]


def rows(out_dir):
    seen = set()
    for p in sorted(glob.glob(os.path.join(out_dir, "part_*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue          # tolerate a torn line from a killed run
                did = r.get("design_id")
                if did is None or did in seen:
                    continue
                seen.add(did)
                yield r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="details")
    ap.add_argument("--dest", default="csv")
    ap.add_argument("--max-rows", type=int, default=400_000,
                    help="rows per CSV file (keeps each well under 100MB)")
    ap.add_argument("--no-description", action="store_true",
                    help="drop the description column (~20% of the bytes)")
    args = ap.parse_args()

    cols = [c for c in COLUMNS
            if not (args.no_description and c == "description")]
    os.makedirs(args.dest, exist_ok=True)

    n = part = 0
    w = fh = None
    for r in rows(args.out):
        if w is None or n >= args.max_rows:
            if fh:
                fh.close()
            part += 1
            n = 0
            path = os.path.join(args.dest, f"cults3d_{part:03d}.csv")
            fh = open(path, "w", newline="", encoding="utf-8-sig")
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            print(f"[writing] {path}")
        out = {}
        for c in cols:
            v = r.get(c)
            out[c] = "|".join(map(str, v)) if isinstance(v, list) else v
        w.writerow(out)
        n += 1
    if fh:
        fh.close()

    total = sum(1 for _ in rows(args.out))
    print(f"\n[done] {total:,} unique models -> {part} csv file(s) in {args.dest}/")


if __name__ == "__main__":
    main()
