"""Fetch per-model detail for every URL in the index.

Storage/resume design is carried over from the sibling cgtrader-scraper, which
proved it under 8-way parallelism:

  - append-only JSONL, deduped by design id on read, so a killed run never
    corrupts anything and simply resumes;
  - sharded at a fixed row count so no file approaches GitHub's 100MB limit;
  - each parallel worker owns its own `part_<tag>_NNNNN.jsonl` series, so
    concurrent workers on the same slice never append to one file and merging
    is a pure file-add rather than a line-level reconciliation.
"""
import argparse
import glob
import gzip
import itertools
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

from cults_parse import parse_model
from cults_session import make_session

# Sized to roughly one CI chunk: at the safe ~2 req/s a 40-minute chunk yields
# ~4,800 rows. Rolling at 5,000 means a shard file is usually finished by the
# time it is committed, instead of being re-committed at four growing sizes and
# leaving three redundant copies in git history. Also keeps every file ~6MB,
# far under GitHub's 100MB limit.
SHARD_MAX_ROWS = 5_000

# Sentinel for "this model is permanently gone", so it can be counted apart
# from transient failures - the two mean very different things when deciding
# whether a run is finished.
GONE = object()


class Throttle:
    """Adaptive global pacing: back off hard on 429, recover slowly.

    The standing instruction on this project is that getting the IP blocked is
    worse than being slow, so the penalty is multiplicative and the recovery is
    additive - it gives ground fast and takes it back gradually.
    """

    def __init__(self, stats, base=0.5, ceiling=8.0):
        self.gap = base
        self.floor = base          # never speed up past the measured-safe rate
        self.ceiling = ceiling
        self.stats = stats
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            due = max(now, self.next_at)
            self.next_at = due + self.gap
        delay = due - time.time()
        if delay > 0:
            time.sleep(delay)

    def penalise(self):
        with self.lock:
            self.gap = min(self.ceiling, max(self.floor * 2, self.gap * 2))
            self.stats["throttled"] += 1
            print(f"  [429] backing off -> {self.gap:.2f}s/request", flush=True)

    def ok(self):
        # Decay geometrically, not by a fixed step. The penalty is
        # multiplicative, so a bad patch can push the gap to the 8s ceiling -
        # and backing off 0.01 per success would then need ~750 successes to
        # get home, which at 0.125 req/s is over an hour of crawling. x0.97
        # recovers from the ceiling in ~90 successes while still being far
        # slower to speed up than to slow down.
        if self.gap > self.floor:
            with self.lock:
                self.gap = max(self.floor, self.gap * 0.97)


class ShardedWriter:
    """Append-only writer that rolls to a new file every max_rows."""

    def __init__(self, out_dir, tag=None, max_rows=SHARD_MAX_ROWS):
        self.dir = out_dir
        os.makedirs(self.dir, exist_ok=True)
        self.max_rows = max_rows
        self.tag = tag
        self.lock = threading.Lock()
        # Anchored so an untagged writer claims only part_00001.jsonl and never
        # mistakes another worker's part_s3_00001.jsonl for its own.
        self._mine = re.compile(
            r"^part_\d+\.jsonl$" if tag is None
            else rf"^part_{re.escape(tag)}_\d+\.jsonl$")
        own = sorted(p for p in glob.glob(os.path.join(self.dir, "part_*.jsonl"))
                     if self._mine.match(os.path.basename(p)))
        if own:
            self.path = own[-1]
            self.index = len(own)
            with open(self.path, encoding="utf-8") as f:
                self.count = sum(1 for _ in f)
        else:
            self.index = 1
            self.path = os.path.join(self.dir, self._name(self.index))
            self.count = 0
        self.fh = open(self.path, "a", encoding="utf-8")

    def _name(self, i):
        pre = "part_" if self.tag is None else f"part_{self.tag}_"
        return f"{pre}{i:05d}.jsonl"

    def write(self, row):
        with self.lock:
            if self.count >= self.max_rows:
                self.fh.close()
                self.index += 1
                self.path = os.path.join(self.dir, self._name(self.index))
                self.fh = open(self.path, "a", encoding="utf-8")
                self.count = 0
            self.fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.count += 1

    def flush(self):
        with self.lock:
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        with self.lock:
            self.fh.close()


def load_done(out_dir):
    """Every slug already scraped, across all workers' shards."""
    done = set()
    for p in glob.glob(os.path.join(out_dir, "part_*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue          # tolerate a torn last line from a kill
                if row.get("slug"):
                    done.add(row["slug"])
    return done


def load_index(index_dir):
    """Read the compact index as (group, slug) tuples.

    Held as tuples, not dicts, and the URL is built per request rather than
    stored: at 2.92M rows the dict-of-three-strings version cost 1.25GB
    resident and ~28s to load, which is a lot to carry for weeks and to redo
    every chunk. Interning the group (only ~18 distinct values) drops it
    further.
    """
    rows = []
    for p in sorted(glob.glob(os.path.join(index_dir, "urls_*.tsv.gz"))):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 2:
                    rows.append((sys.intern(parts[0]), parts[1]))
    return rows


def model_url(group, slug):
    return f"https://cults3d.com/en/3d-model/{group}/{slug}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="url_index")
    ap.add_argument("--out", default="details")
    ap.add_argument("--shard", default="1/1",
                    help="I/N - this worker takes every Nth url starting at I-1")
    # Measured 2026-09-22 over sustained 100s windows against one IP:
    #   1 worker unpaced      1.75 req/s   0% 429
    #   2 workers unpaced     2.14 req/s  78% 429
    #   4 workers @0.25s gap  2.17 req/s  44% 429
    #   4 workers @0.50s gap  2.00 req/s   0% 429   <- default
    # The per-IP ceiling is ~2 req/s no matter how many threads, so throughput
    # comes from running on more IPs (CI runners), never from more threads.
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--gap", type=float, default=0.5,
                    help="global seconds between requests (the real rate limit)")
    ap.add_argument("--delay", type=float, default=0.05,
                    help="extra per-worker jitter on top of --gap")
    ap.add_argument("--max-seconds", type=int, default=0,
                    help="stop cleanly after N seconds (for CI chunking)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-shuffle", dest="shuffle", action="store_false",
                    help="scrape in sitemap order (oldest designs first)")
    ap.add_argument("--seed", type=int, default=20260922,
                    help="shuffle seed; must not change mid-run")
    args = ap.parse_args()

    i, n = (int(x) for x in args.shard.split("/"))
    tag = None if n == 1 else f"s{i}of{n}"

    urls = load_index(args.index)
    if not urls:
        sys.exit(f"no url index found in {args.index}/ - run build_url_index.py first")

    if args.shuffle:
        # The sitemaps are ordered oldest-design-first, so scraping in place
        # means that for the whole multi-week run the data on disk is nothing
        # but the oldest models - useless for looking at partway through.
        # Shuffling makes any prefix a representative sample of the catalogue.
        #
        # The seed is FIXED and the shuffle happens before sharding, so the
        # order is identical on every restart and across machines. That is what
        # keeps resume correct and keeps --shard slices disjoint; a random seed
        # here would silently re-scrape and leave gaps.
        random.Random(args.seed).shuffle(urls)

    mine = urls[i - 1::n]
    done = load_done(args.out)
    todo = [u for u in mine if u[1] not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"[shard {i}/{n}] index={len(urls):,} mine={len(mine):,} "
          f"done={len(done):,} todo={len(todo):,}", flush=True)
    if not todo:
        print("[done] nothing left in this shard")
        return

    writer = ShardedWriter(args.out, tag=tag)
    session = make_session()
    t0 = time.time()
    stats = {"ok": 0, "fail": 0, "gone": 0, "last_report": 0,
             "resolves": 0, "throttled": 0}
    stop = threading.Event()
    lock = threading.Lock()
    throttle = Throttle(stats, base=args.gap)

    resolve_lock = threading.Lock()
    last_resolve = [0.0]

    def resolve():
        """Re-solve the challenge at most once per 60s across all threads.

        Without the cooldown, a burst of challenged responses would launch one
        browser per thread; they would all be solving the same thing.
        """
        with resolve_lock:
            if time.time() - last_resolve[0] < 60:
                time.sleep(3)
                return
            print("  [challenge] re-solving in browser...", flush=True)
            fresh = make_session(force=True)
            session.headers.update(fresh.headers)
            # REPLACE the jar, never update it. The cached cookies go in
            # domain-less while a browser export carries ".cults3d.com", so
            # update() kept both copies of cf_clearance and requests sent them
            # together. Cloudflare saw a conflicting clearance, re-challenged,
            # and the next re-solve added a third - a loop that cost ~10
            # browser launches an hour and about 6% of throughput.
            session.cookies.clear()
            session.cookies.update(fresh.cookies)
            last_resolve[0] = time.time()
            stats["resolves"] += 1

    def fetch(url):
        """One model, with the right response to each failure mode.

        Measured on 2026-09-22: 8 concurrent requests is clean, 16 earns HTTP
        429. So 429 is a real signal to slow the whole crawl down, not to
        re-solve the challenge - a browser re-solve costs ~7s and fixes nothing
        here. Only an actual challenge response justifies the browser.
        """
        for attempt in range(4):
            throttle.wait()
            r = session.get(url, timeout=30)

            if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
                throttle.ok()
                return r
            if r.status_code in (404, 410):
                # 410 Gone is as permanent as 404. Letting it fall through to
                # the generic retry path burned four throttle slots per dead
                # model, and ~3.4% of the catalogue is dead.
                return GONE
            if r.status_code == 429:
                throttle.penalise()
                time.sleep(min(60, 5 * 2 ** attempt) + random.uniform(0, 3))
                continue
            if r.status_code == 403 or r.headers.get("Cf-Mitigated") == "challenge" \
                    or "Just a moment" in r.text[:2000]:
                resolve()                        # cooled-down, single-flight
                continue
            time.sleep(2 * (attempt + 1))
        return None

    def work(item):
        if stop.is_set():
            return
        group, slug = item
        url = model_url(group, slug)
        time.sleep(random.uniform(args.delay * 0.5, args.delay * 1.5))
        try:
            r = fetch(url)
            if r is GONE:
                with lock:
                    stats["gone"] += 1
                return
            if r is None:
                with lock:
                    stats["fail"] += 1
                return
            row = parse_model(r.text, url)
            row["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            writer.write(row)
            with lock:
                stats["ok"] += 1
                el = time.time() - t0
                if stats["ok"] - stats["last_report"] >= 200:
                    stats["last_report"] = stats["ok"]
                    rate = stats["ok"] / el
                    left = (len(todo) - stats["ok"]) / rate / 3600 if rate else 0
                    print(f"  {stats['ok']:>7,}/{len(todo):,}  "
                          f"gone={stats['gone']:<5} fail={stats['fail']:<5} "
                          f"{rate:5.2f} req/s  eta {left:5.1f}h", flush=True)
                    writer.flush()
        except Exception as e:
            with lock:
                stats["fail"] += 1
                if stats["fail"] % 50 == 1:
                    print(f"  ! {type(e).__name__}: {str(e)[:80]}", flush=True)

        if args.max_seconds and time.time() - t0 > args.max_seconds:
            stop.set()

    # Workers PULL from a shared cursor rather than ThreadPoolExecutor.map().
    # map() submits every item up front: at 2.92M models that is 2.92M Future
    # objects created before the first fetch, which is what drove each scraper
    # process to ~5GB resident. Pulling keeps it flat regardless of catalogue
    # size, and makes --max-seconds stop immediately instead of unwinding
    # millions of queued futures.
    cursor = itertools.count()
    cursor_lock = threading.Lock()

    def pump():
        while not stop.is_set():
            with cursor_lock:
                k = next(cursor)
            if k >= len(todo):
                return
            work(todo[k])

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda _: pump(), range(args.workers)))
    except KeyboardInterrupt:
        stop.set()
        print("\n[interrupted] flushing...")
    finally:
        writer.flush()
        writer.close()

    el = time.time() - t0
    print(f"\n[shard {i}/{n}] ok={stats['ok']:,} fail={stats['fail']:,} "
          f"elapsed={el/60:.1f}m rate={stats['ok']/el if el else 0:.2f} req/s")


if __name__ == "__main__":
    main()
