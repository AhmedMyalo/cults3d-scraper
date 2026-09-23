"""Collect the Cults3D catalogue through the official GraphQL API.

Measured facts this is built on (not assumptions - each was verified):
  - creationsBatch queries the whole site, 100 results maximum per request
  - offset is capped at 50,000, so no single filter can reach past it
  - the limit is 60 requests per 60s window, per x-ratelimit-* headers
  - the API reports 3,599,419 creations; our sitemap crawl found 2,922,332,
    so the sitemaps were missing ~677k

Two design rules carried from how the scraping attempt went wrong:
  - A 429 is a FULL STOP, not something to back off from and continue.
  - Nothing about the schema is guessed. Nested object fields are discovered by
    introspection at startup and the query is built from what is actually there,
    because inventing an enum value (BY_PUBLISHED_AT) and a page size already
    cost us two wasted runs.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

import requests

ENDPOINT = "https://cults3d.com/graphql"
OFFSET_CAP = 50_000
PAGE = 100
SHARD_MAX_ROWS = 20_000

# Scalar fields confirmed present on Creation.
SCALARS = ["identifier", "slug", "name", "description", "details",
           "publishedAt", "updatedAt", "viewsCount", "likesCount",
           "downloadsCount", "madeWithAi", "safe", "featured", "openPriced",
           "url", "shortUrl", "illustrationImageUrl", "visibility"]
# Object/list fields whose sub-selection is discovered, never guessed.
OBJECTS = ["price", "totalSalesAmount", "creator", "license", "category",
           "tags", "comments", "makes", "collections", "subCategories",
           "usages"]


class Stop(Exception):
    """Raised on 429 or any refusal - the run ends, it does not adapt."""


class Api:
    def __init__(self, user, key, rpm=55):
        self.s = requests.Session()
        self.s.auth = (user, key)
        self.s.headers.update({"Content-Type": "application/json",
                               "User-Agent": "cults3d-dataset/1.0"})
        self.gap = 60.0 / rpm          # stay just under the published 60/min
        self.next_at = 0.0
        self.count = 0

    def __call__(self, query, variables=None):
        wait = self.next_at - time.time()
        if wait > 0:
            time.sleep(wait)
        self.next_at = time.time() + self.gap
        self.count += 1
        r = self.s.post(ENDPOINT, json={"query": query,
                                        "variables": variables or {}},
                        timeout=90)
        # Always carry the body and the rate headers into the message. A 403
        # on request 1 told us nothing last time and left us guessing whether
        # the key was revoked or the daily quota was simply still spent.
        rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
        if r.status_code == 429:
            raise Stop(f"HTTP 429 after {self.count} requests | {rl} | "
                       f"{r.text[:300]}")
        if r.status_code == 403:
            raise Stop(f"HTTP 403 after {self.count} requests | {rl} | "
                       f"body: {r.text[:300]}")
        if r.status_code != 200:
            raise Stop(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("errors"):
            raise Stop("GraphQL errors: " + json.dumps(body["errors"])[:300])
        return body["data"]


def discover_selection(api):
    """Build the sub-selection for each object field by reading the schema."""
    typemap = api("""
    { __type(name: "Creation") { fields { name type {
        name kind ofType { name kind ofType { name kind ofType { name } } } } } } }
    """)["__type"]["fields"]

    def base(t):
        while t and not t.get("name"):
            t = t.get("ofType")
        return (t or {}).get("name")

    wanted = {f["name"]: base(f["type"]) for f in typemap
              if f["name"] in OBJECTS}

    parts = []
    for fname, tname in wanted.items():
        if not tname:
            continue
        sub = api('{ __type(name: "%s") { fields { name type { name kind '
                  'ofType { name kind } } } } }' % tname)["__type"]
        # A scalar/enum type comes back with fields = null, not an empty list;
        # iterating that is a TypeError, which is how the first run died while
        # the workflow still reported success.
        if not sub or not sub.get("fields"):
            parts.append(fname)
            continue
        # Keep only leaf scalars - nesting deeper multiplies response size for
        # little gain, and lists are only needed for their length.
        leaves = [f["name"] for f in sub["fields"]
                  if (f["type"].get("kind") == "SCALAR"
                      or (f["type"].get("ofType") or {}).get("kind") == "SCALAR")]
        if not leaves:
            continue
        keep = [l for l in leaves if l in
                ("cents", "currency", "amount", "value", "nick", "name",
                 "slug", "id", "identifier", "code", "text")] or leaves[:3]
        parts.append(f"{fname} {{ {' '.join(dict.fromkeys(keep))} }}")
    return " ".join(SCALARS) + " " + " ".join(parts)


def count_in_window(api, after, before):
    q = ("query($a: ISO8601DateTime, $b: ISO8601DateTime) { creationsBatch("
         "limit: 1, submittedAfter: $a, submittedBefore: $b) { total } }")
    return api(q, {"a": after.isoformat(), "b": before.isoformat()})["creationsBatch"]["total"]


def split_windows(api, start, end, out_path):
    """Bisect the timeline until every window holds fewer than the offset cap."""
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            saved = json.load(f)
        print(f"[windows] reusing {len(saved)} saved windows")
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b), n)
                for a, b, n in saved]

    windows, queue = [], [(start, end)]
    while queue:
        a, b = queue.pop(0)
        n = count_in_window(api, a, b)
        if n == 0:
            continue
        if n < OFFSET_CAP or (b - a) <= timedelta(days=1):
            windows.append((a, b, n))
            print(f"[window] {a:%Y-%m-%d} -> {b:%Y-%m-%d}  {n:>7,}")
        else:
            mid = a + (b - a) / 2
            queue.insert(0, (mid, b))
            queue.insert(0, (a, mid))
    windows.sort(key=lambda w: w[0])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([[a.isoformat(), b.isoformat(), n] for a, b, n in windows], f)
    print(f"[windows] {len(windows)} windows, {sum(w[2] for w in windows):,} models")
    return windows


def load_done(out_dir):
    done = set()
    for p in glob.glob(os.path.join(out_dir, "part_*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["identifier"])
                except Exception:
                    continue
    return done


class Writer:
    def __init__(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        self.dir, self.n = out_dir, 0
        existing = sorted(glob.glob(os.path.join(out_dir, "part_*.jsonl")))
        self.idx = len(existing) or 1
        self.path = existing[-1] if existing else os.path.join(
            out_dir, f"part_{self.idx:05d}.jsonl")
        self.n = sum(1 for _ in open(self.path, encoding="utf-8")) \
            if os.path.exists(self.path) else 0
        self.fh = open(self.path, "a", encoding="utf-8")

    def write(self, row):
        if self.n >= SHARD_MAX_ROWS:
            self.fh.close()
            self.idx += 1
            self.path = os.path.join(self.dir, f"part_{self.idx:05d}.jsonl")
            self.fh = open(self.path, "a", encoding="utf-8")
            self.n = 0
        self.fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.n += 1

    def close(self):
        self.fh.flush()
        self.fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="api_data")
    ap.add_argument("--rpm", type=int, default=55)
    ap.add_argument("--max-seconds", type=int, default=0)
    args = ap.parse_args()

    user, key = os.environ.get("CULTS_USER"), os.environ.get("CULTS_KEY")
    if not user or not key:
        sys.exit("CULTS_USER / CULTS_KEY not set")

    api = Api(user, key, args.rpm)
    t0 = time.time()
    os.makedirs(args.out, exist_ok=True)

    selection = discover_selection(api)
    print(f"[schema] selection built from introspection:\n  {selection[:300]}...\n")

    start = datetime(2013, 1, 1, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    windows = split_windows(api, start, end,
                            os.path.join(args.out, "windows.json"))

    done = load_done(args.out)
    print(f"[resume] {len(done):,} already collected\n")
    writer = Writer(args.out)
    added = 0

    try:
        for a, b, n in windows:
            for off in range(0, min(n, OFFSET_CAP), PAGE):
                q = ("query($a: ISO8601DateTime, $b: ISO8601DateTime, $o: Int!) {"
                     " creationsBatch(limit: %d, offset: $o, submittedAfter: $a,"
                     " submittedBefore: $b, sort: BY_PUBLICATION, direction: ASC)"
                     " { total results { %s } } }" % (PAGE, selection))
                data = api(q, {"a": a.isoformat(), "b": b.isoformat(), "o": off})
                rows = data["creationsBatch"]["results"]
                if not rows:
                    break
                for r in rows:
                    if r.get("identifier") in done:
                        continue
                    done.add(r.get("identifier"))
                    for lf in ("comments", "makes", "collections", "tags",
                               "subCategories", "usages"):
                        if isinstance(r.get(lf), list):
                            r[f"{lf}_count"] = len(r[lf])
                    r["fetched_at"] = datetime.now(timezone.utc).isoformat()
                    writer.write(r)
                    added += 1
                if added and added % 1000 < PAGE:
                    el = time.time() - t0
                    print(f"  {added:>8,} new | {api.count:>6,} requests | "
                          f"{added/el*60:.0f} models/min", flush=True)
                if args.max_seconds and time.time() - t0 > args.max_seconds:
                    raise KeyboardInterrupt
    except Stop as e:
        print(f"\n[STOPPED] {e}")
        print("[STOPPED] Not retrying and not backing off - this needs a decision.")
    except KeyboardInterrupt:
        print("\n[time budget reached, stopping cleanly]")
    finally:
        writer.close()
        el = time.time() - t0
        print(f"\n[done] +{added:,} models, {api.count:,} requests, "
              f"{el/60:.1f} min, total collected {len(done):,}")


if __name__ == "__main__":
    main()
