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

SHARD_MAX_ROWS = 20_000


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
        if self.gap > self.floor:
            with self.lock:
                self.gap = max(self.floor, self.gap - 0.002)


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
    rows = []
    for p in sorted(glob.glob(os.path.join(index_dir, "urls_*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


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
    args = ap.parse_args()

    i, n = (int(x) for x in args.shard.split("/"))
    tag = None if n == 1 else f"s{i}of{n}"

    urls = load_index(args.index)
    if not urls:
        sys.exit(f"no url index found in {args.index}/ - run build_url_index.py first")
    mine = urls[i - 1::n]
    done = load_done(args.out)
    todo = [u for u in mine if u["slug"] not in done]
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
    stats = {"ok": 0, "fail": 0, "last_report": 0, "resolves": 0, "throttled": 0}
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
            session.cookies.update(fresh.cookies)
            last_resolve[0] = time.time()
            stats["resolves"] += 1

    def fetch(item):
        """One model, with the right response to each failure mode.

        Measured on 2026-09-22: 8 concurrent requests is clean, 16 earns HTTP
        429. So 429 is a real signal to slow the whole crawl down, not to
        re-solve the challenge - a browser re-solve costs ~7s and fixes nothing
        here. Only an actual challenge response justifies the browser.
        """
        for attempt in range(4):
            throttle.wait()
            r = session.get(item["url"], timeout=30)

            if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
                throttle.ok()
                return r
            if r.status_code == 404:
                return None                      # model vanished; not an error
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
        time.sleep(random.uniform(args.delay * 0.5, args.delay * 1.5))
        try:
            r = fetch(item)
            if r is None:
                with lock:
                    stats["fail"] += 1
                return
            row = parse_model(r.text, item["url"])
            row["lastmod"] = item.get("lastmod")
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
                          f"fail={stats['fail']:<5} {rate:5.2f} req/s  "
                          f"eta {left:5.1f}h", flush=True)
                    writer.flush()
        except Exception as e:
            with lock:
                stats["fail"] += 1
                if stats["fail"] % 50 == 1:
                    print(f"  ! {type(e).__name__}: {str(e)[:80]}", flush=True)

        if args.max_seconds and time.time() - t0 > args.max_seconds:
            stop.set()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(work, todo))
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
