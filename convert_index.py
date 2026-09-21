"""One-off: convert a legacy JSONL url_index to the compact gzipped TSV format.

The first index build wrote full JSONL rows (group, slug, url, lastmod) and came
to 524MB, which is too much to carry in the repo alongside the scrape output.
The URL is derivable from group + slug and published_at comes off the model
page, so only those two fields are actually needed.
"""
import glob
import gzip
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

src_files = sorted(glob.glob("url_index/urls_*.jsonl"))
if not src_files:
    sys.exit("no legacy urls_*.jsonl found - nothing to convert")

before = sum(os.path.getsize(p) for p in src_files)
rows = 0

for p in src_files:
    dest = p.replace(".jsonl", ".tsv.gz")
    with open(p, encoding="utf-8") as fin, \
            gzip.open(dest + ".tmp", "wt", encoding="utf-8") as fout:
        for line in fin:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("group") and r.get("slug"):
                fout.write(f"{r['group']}\t{r['slug']}\n")
                rows += 1
    os.replace(dest + ".tmp", dest)
    os.remove(p)

after = sum(os.path.getsize(p) for p in glob.glob("url_index/urls_*.tsv.gz"))
print(f"converted {len(src_files)} files, {rows:,} rows")
print(f"{before/1e6:,.0f} MB -> {after/1e6:,.0f} MB "
      f"({100*after/before:.1f}% of original)")
