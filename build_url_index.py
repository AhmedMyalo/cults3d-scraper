"""Build the master list of every Cults3D model URL from the site's own sitemaps.

This replaces the category-pagination crawl the sibling project needed. Cults3D
publishes 403 English `creationsN.xml.gz` sitemaps of 10,000 URLs each, so the
whole catalogue is enumerable in ~403 gzipped requests with no pagination, no
per-listing row cap, and no dependence on a stable sort order or on which
listing filters happen to be on by default.

Output: url_index/urls_NNNN.tsv.gz ("<group>\t<slug>" per line) plus meta.json.

The format is deliberately minimal. Written as JSONL with the full URL and
lastmod it came to 524MB, which is too much to carry in a git repo that also
has to hold the scrape output. The URL is fully derivable from group + slug,
and published_at is read off the model page anyway, so storing either here is
pure duplication. Compact + gzipped lands around 45MB.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from cults_session import get, make_session

SITEMAP_INDEX = "https://cults3d.com/sitemap.xml"
OUT_DIR = "url_index"
# A model page is /<lang>/3d-model/<group>/<slug>. Anything else in the
# creations sitemaps (design-collections pages etc.) is not a model.
MODEL_RE = re.compile(r"^https://cults3d\.com/en/3d-model/([a-z0-9\-]+)/([^/?#]+)$")


def list_creation_sitemaps(session, lang="en"):
    xml = get(session, SITEMAP_INDEX).text
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    pat = re.compile(rf"/{lang}/creations(\d+)\.xml\.gz$")
    out = [(int(pat.search(l).group(1)), l) for l in locs if pat.search(l)]
    return [l for _, l in sorted(out)]


def parse_sitemap(session, url, retries=3):
    """Fetch one gzipped sitemap and yield (group, slug, url, lastmod)."""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            try:
                xml = gzip.decompress(r.content).decode("utf-8")
            except (OSError, gzip.BadGzipFile):
                xml = r.text          # server already decompressed it
            break
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt+1} on {url}: {type(e).__name__} {e}")
            time.sleep(5 * (attempt + 1))

    # <url><loc>..</loc><lastmod>..</lastmod></url>
    for blk in re.finditer(r"<url>(.*?)</url>", xml, re.S):
        body = blk.group(1)
        loc = re.search(r"<loc>(.*?)</loc>", body)
        if not loc:
            continue
        m = MODEL_RE.match(loc.group(1))
        if not m:
            continue
        lastmod = re.search(r"<lastmod>(.*?)</lastmod>", body)
        yield {
            "group": m.group(1),
            "slug": m.group(2),
            "url": loc.group(1),
            "lastmod": lastmod.group(1) if lastmod else None,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N sitemaps (for testing)")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    session = make_session()

    sitemaps = list_creation_sitemaps(session, args.lang)
    if args.limit:
        sitemaps = sitemaps[:args.limit]
    print(f"[index] {len(sitemaps)} '{args.lang}' creation sitemaps to read")

    seen = set()
    total = 0
    t0 = time.time()
    for i, sm in enumerate(sitemaps, 1):
        # Resumable: a finished sitemap has its own output file.
        part = os.path.join(args.out, f"urls_{i:04d}.tsv.gz")
        if os.path.exists(part):
            with gzip.open(part, "rt", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) == 2:
                        seen.add(parts[1])
                        total += 1
            continue

        rows = list(parse_sitemap(session, sm))
        tmp = part + ".tmp"
        n_new = 0
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for row in rows:
                if row["slug"] in seen:
                    continue           # same model can appear under >1 group
                seen.add(row["slug"])
                f.write(f"{row['group']}\t{row['slug']}\n")
                n_new += 1
        os.replace(tmp, part)
        total += n_new

        el = time.time() - t0
        rate = i / el if el else 0
        eta = (len(sitemaps) - i) / rate / 60 if rate else 0
        print(f"[{i:>4}/{len(sitemaps)}] +{n_new:>5} new  total={total:>9,}  "
              f"{rate:4.2f} sitemap/s  eta {eta:5.1f}m", flush=True)
        time.sleep(args.delay)

    meta = {
        "lang": args.lang,
        "sitemaps": len(sitemaps),
        "unique_models": total,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"\n[done] {total:,} unique model URLs -> {args.out}/")


if __name__ == "__main__":
    main()
