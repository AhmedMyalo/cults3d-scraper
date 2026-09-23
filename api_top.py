"""Collect the top-downloaded recent models per category.

Chosen against measured numbers, not estimates: 30,000 per category over the
last 3 years is ~265,600 models and ~2,660 requests, so about 5.3 days at the
measured 500/day cap.

Why the top rather than a slice of everything: 86% of recent uploads are paid
and most have almost no downloads, so a proportional sample would be mostly
rows with nothing to teach. The question being asked is "what sells", and that
lives in the winners.

Skips anything already held, from BOTH earlier collections, matched on slug:
  details/   - 49,939 models from the scraping phase
  api_data/  - 26,571 models from the first API run
Note this avoids duplicate rows, not requests: results still arrive 100 to a
page whether or not we already have them.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from api_scrape import Api, Stop, Writer, discover_selection

OFFSET_CAP = 50_000
PAGE = 100
# Do NOT hand-write this. api_scrape.discover_selection reads the real schema
# at runtime; hardcoding it here is exactly how this script died on its first
# run, guessing that Comment has an `id` field. It does not.


def known_slugs():
    """Every slug already collected, from both earlier phases."""
    seen = set()
    for pattern, key in (("details/part_*.jsonl", "slug"),
                         ("api_data/part_*.jsonl", "slug"),
                         ("top_data/part_*.jsonl", "slug")):
        for p in glob.glob(pattern):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        v = json.loads(line).get(key)
                    except Exception:
                        continue
                    if v:
                        seen.add(v)
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="top_data")
    ap.add_argument("--per-category", type=int, default=30_000)
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--max-seconds", type=int, default=19_800)
    args = ap.parse_args()

    user, key = os.environ.get("CULTS_USER"), os.environ.get("CULTS_KEY")
    if not user or not key:
        sys.exit("CULTS_USER / CULTS_KEY not set")

    api = Api(user, key)
    os.makedirs(args.out, exist_ok=True)
    state_path = os.path.join(args.out, "progress.json")
    state = json.load(open(state_path, encoding="utf-8")) \
        if os.path.exists(state_path) else {}

    counts = json.load(open("diagnostics/category_counts.json", encoding="utf-8"))
    cats = [c for c in counts["categories"] if c["recent_paid"] > 0]
    cats.sort(key=lambda c: -c["recent_paid"])
    since = (datetime.now(timezone.utc)
             - timedelta(days=365 * args.years)).replace(microsecond=0).isoformat()

    have = known_slugs()
    print(f"[skip] {len(have):,} slugs already held from earlier phases")

    quota_stop = [False]
    writer = Writer(args.out)
    t0, added, skipped = time.time(), 0, 0
    finished = True

    try:
        # Inside the try: a Stop raised here used to escape uncaught, which is
        # why a spent quota showed up as a red failed job rather than a clean
        # "come back tomorrow".
        fields = discover_selection(api)
        print(f"[schema] selection built by introspection "
              f"({len(fields.split())} tokens)")
        for c in cats:
            slug = c["slug"]
            target = min(args.per_category, c["recent_paid"], OFFSET_CAP)
            off = state.get(slug, 0)
            if off >= target:
                continue
            print(f"\n[{slug}] target {target:,}, resuming at offset {off:,}")
            while off < target:
                q = ("query($a: ISO8601DateTime, $c: String, $o: Int!) {"
                     " creationsBatch(limit: %d, offset: $o, submittedAfter: $a,"
                     " categorySlugEn: $c, onlyPriced: true,"
                     " sort: BY_DOWNLOADS, direction: DESC)"
                     " { results { %s } } }" % (PAGE, fields))
                rows = api(q, {"a": since, "c": slug, "o": off})["creationsBatch"]["results"]
                if not rows:
                    break
                for r in rows:
                    if r.get("slug") in have:
                        skipped += 1
                        continue
                    have.add(r.get("slug"))
                    for lf in ("comments", "makes", "collections", "tags"):
                        if isinstance(r.get(lf), list):
                            r[f"{lf}_count"] = len(r[lf])
                            if lf != "tags":
                                r.pop(lf)
                    r["source_category"] = slug
                    r["fetched_at"] = datetime.now(timezone.utc).isoformat()
                    writer.write(r)
                    added += 1
                off += PAGE
                state[slug] = off
                if off % 2000 == 0:
                    print(f"  {slug}: {off:,}/{target:,} | +{added:,} new | "
                          f"{skipped:,} already held", flush=True)
                if time.time() - t0 > args.max_seconds:
                    finished = False
                    raise KeyboardInterrupt
            print(f"[{slug}] done at {off:,}")
    except Stop as e:
        finished = False
        msg = str(e)
        print(f"\n[STOPPED] {msg}")
        if "429" in msg or "403" in msg:
            # The daily allowance being spent is the expected way a run ends,
            # not a fault. Exiting 0 keeps GitHub from marking the job red and
            # emailing him about something working exactly as designed.
            print("[STOPPED] Quota refusal, which is normal. Nothing is "
                  "broken; the next run resumes from here.")
            quota_stop[0] = True
        else:
            print("[STOPPED] Not a quota refusal, so this one needs a look.")
    except KeyboardInterrupt:
        print("\n[time budget reached]")
    finally:
        writer.close()
        json.dump(state, open(state_path, "w", encoding="utf-8"), indent=2)
        remaining = sum(max(0, min(args.per_category, c["recent_paid"], OFFSET_CAP)
                            - state.get(c["slug"], 0)) for c in cats)
        done_all = remaining == 0
        print(f"\n[done] +{added:,} new | {skipped:,} already held | "
              f"{api.count} requests | {(time.time()-t0)/60:.1f} min")
        print(f"[progress] {remaining:,} model-slots still to go")
        # The workflow reads this to decide whether to raise the finish notice.
        with open(os.path.join(args.out, "COMPLETE"), "w") as f:
            f.write("yes" if done_all else "no")
    if quota_stop[0]:
        sys.exit(0)


if __name__ == "__main__":
    main()
