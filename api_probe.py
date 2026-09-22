"""Find out what the official Cults3D GraphQL API can actually deliver.

The whole paid-vs-free decision rests on one unknown: how many creations a
single query returns. I have been quoting "50-100 per request" as if it were a
fact; it is a guess, and guessing is what cost us the IP block.

This spends as few requests as possible out of the ~500/day budget:
  1. one introspection query - what fields and pagination arguments exist
  2. a few probes at increasing page sizes, stopping at the first refusal

Credentials come from the environment, never from a file:
    CULTS_USER=<username>  CULTS_KEY=<api key>  python api_probe.py
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import requests

ENDPOINT = "https://cults3d.com/graphql"
USER = os.environ.get("CULTS_USER", "")
KEY = os.environ.get("CULTS_KEY", "")

if not USER or not KEY:
    sys.exit("set CULTS_USER and CULTS_KEY in the environment first")

session = requests.Session()
session.auth = (USER, KEY)
session.headers.update({"Content-Type": "application/json",
                        "User-Agent": "cults3d-dataset-probe/1.0"})

used = 0


def gql(query, variables=None, label=""):
    """One API call, with the block/limit responses treated as hard stops."""
    global used
    used += 1
    r = session.post(ENDPOINT,
                     json={"query": query, "variables": variables or {}},
                     timeout=60)
    if r.status_code == 429:
        sys.exit(f"[stop] 429 rate limited after {used} requests - not retrying")
    if r.status_code == 403 or "access denied" in r.text[:500].lower():
        sys.exit(f"[stop] 403 / blocked after {used} requests - not retrying")
    if r.status_code != 200:
        print(f"  [{label}] HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    if body.get("errors"):
        print(f"  [{label}] errors: "
              f"{json.dumps(body['errors'])[:300]}")
    return body


# --- 1. what does the schema even offer? ------------------------------
print("[1] introspecting the root query type...")
INTROSPECT = """
{ __schema { queryType { fields { name
      args { name type { name kind ofType { name } } }
      type { name kind ofType { name } } } } } }
"""
d = gql(INTROSPECT, label="introspect")
if d and d.get("data"):
    fields = d["data"]["__schema"]["queryType"]["fields"]
    print(f"    {len(fields)} root queries available:\n")
    for f in fields:
        args = ", ".join(a["name"] for a in f.get("args", []))
        print(f"      {f['name']}({args})")
    with open("diagnostics/api_schema.json", "w", encoding="utf-8") as fh:
        json.dump(fields, fh, indent=2, ensure_ascii=False)
    print("\n    [saved] diagnostics/api_schema.json")

# --- 2. how large a page will it actually serve? ----------------------
# Field names are guessed from the docs; the introspection above is what
# tells us the real ones, so this is best-effort and prints what it learns.
print("\n[2] probing page size (stops at the first refusal)...")
PAGE_Q = """
query($limit: Int!) {
  creations(limit: $limit) {
    id name slug price { cents currency } downloadsCount likesCount viewsCount
  }
}
"""
best = 0
for limit in (10, 50, 100, 200, 500):
    d = gql(PAGE_Q, {"limit": limit}, label=f"limit={limit}")
    if not d:
        break
    rows = (d.get("data") or {}).get("creations")
    if rows is None:
        print(f"    limit={limit:<4} -> rejected (see errors above)")
        break
    print(f"    limit={limit:<4} -> returned {len(rows)}")
    best = max(best, len(rows))
    if len(rows) < limit:
        print("    (server capped it - this is the real maximum)")
        break
    time.sleep(1)

print(f"\n[requests used] {used} of the ~500/day budget")
if best:
    left = 2_872_393
    per_day = 500 * best
    print(f"[max per request] {best}")
    print(f"[throughput]      {per_day:,}/day -> {left/per_day:.0f} days "
          f"for the remaining {left:,}")
