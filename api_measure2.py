"""Second pass: the two numbers still missing, plus the enum I guessed wrong.

  1. the real values of CreationSortEnum  (I invented BY_PUBLISHED_AT and it
     was rejected - introspect instead of guessing, again)
  2. how deep offset pagination goes      (the CGTrader row-cap question)
  3. whether the 60-per-window limit in the headers is per minute or shorter,
     read from the reset timestamp rather than by exhausting it

Also checks whether comments/makes/collections can yield counts, since those
came back as object fields rather than plain Ints.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

import requests

ENDPOINT = "https://cults3d.com/graphql"
USER, KEY = os.environ.get("CULTS_USER", ""), os.environ.get("CULTS_KEY", "")
if not USER or not KEY:
    sys.exit("set CULTS_USER and CULTS_KEY")

s = requests.Session()
s.auth = (USER, KEY)
s.headers.update({"Content-Type": "application/json",
                  "User-Agent": "cults3d-dataset-probe/1.0"})
used = 0
out = {}


def gql(q, v=None, label=""):
    global used
    used += 1
    r = s.post(ENDPOINT, json={"query": q, "variables": v or {}}, timeout=60)
    if r.status_code == 429:
        print(f"\n[STOP] 429 after {used} requests - stopping per the rule")
        out["hit_429_at"] = used
        save()
        sys.exit(0)
    return r


def save():
    os.makedirs("diagnostics", exist_ok=True)
    out["requests_used"] = used
    with open("diagnostics/api_measurements2.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


# --- 1. the sort enum, read not guessed -------------------------------
print("[1] CreationSortEnum values")
r = gql('{ __type(name: "CreationSortEnum") { enumValues { name } } }')
vals = [e["name"] for e in
        ((r.json().get("data") or {}).get("__type") or {}).get("enumValues", [])]
out["sort_values"] = vals
print(f"    {vals}")
sort_val = next((v for v in vals if "PUBLISH" in v.upper()),
                next((v for v in vals if "DATE" in v.upper() or "RECENT" in v.upper()),
                     vals[0] if vals else None))
print(f"    using: {sort_val}")

# --- 2. can we get comment / make / collection counts? ----------------
print("\n[2] do comments / makes / collections expose a count?")
for fname in ("comments", "makes", "collections", "illustrations", "tags",
              "subCategories", "usages"):
    r = gql('{ __type(name: "Creation") { fields { name type { name kind '
            'ofType { name kind ofType { name } } } } } }'
            if fname == "comments" else
            '{ __type(name: "Creation") { fields { name } } }')
    break   # one request is enough; inspect the shape below
tq = '{ __type(name: "Creation") { fields { name type { kind name ofType { kind name ofType { kind name } } } } } }'
r = gql(tq, label="shapes")
shapes = {}
for f in ((r.json().get("data") or {}).get("__type") or {}).get("fields", []):
    t = f["type"]
    chain = []
    while t:
        chain.append(t.get("kind"))
        t = t.get("ofType")
    shapes[f["name"]] = "->".join(c for c in chain if c)
for k in ("comments", "makes", "collections", "tags", "illustrations"):
    print(f"    {k:<16} {shapes.get(k)}")
out["field_shapes"] = shapes

# --- 3. offset depth --------------------------------------------------
print("\n[3] offset depth")
DEEP = ("query($off: Int!, $s: CreationSortEnum) { creationsBatch("
        "limit: 1, offset: $off, sort: $s, direction: ASC) "
        "{ total results { identifier slug } } }")
depth = {}
for off in (0, 10_000, 100_000, 500_000, 1_000_000, 2_000_000, 3_500_000):
    r = gql(DEEP, {"off": off, "s": sort_val}, label=f"off={off}")
    b = r.json()
    if b.get("errors"):
        msg = b["errors"][0].get("message", "")[:100]
        print(f"    offset={off:>9,} -> ERROR: {msg}")
        depth[off] = f"error: {msg}"
        break
    res = ((b.get("data") or {}).get("creationsBatch") or {}).get("results")
    print(f"    offset={off:>9,} -> {'ok' if res else 'EMPTY (cap reached)'}")
    depth[off] = "ok" if res else "empty"
    if not res:
        break
    time.sleep(1)
out["offset_depth"] = depth

# --- 4. what window is the 60 counted over? ---------------------------
print("\n[4] rate-limit window")
r = gql('{ creationsBatch(limit: 1) { total } }')
h = r.headers
reset = h.get("x-ratelimit-reset")
out["rate_headers"] = {k: v for k, v in h.items() if "ratelimit" in k.lower()}
print(f"    limit={h.get('x-ratelimit-limit')} "
      f"remaining={h.get('x-ratelimit-remaining')} reset={reset}")
if reset:
    try:
        t = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        secs = (t - datetime.now(timezone.utc)).total_seconds()
        print(f"    resets in {secs:.0f}s -> window looks like "
              f"{'60s' if secs <= 61 else '30s' if secs <= 31 else f'{secs:.0f}s'}")
        out["seconds_to_reset"] = secs
    except Exception as e:
        print(f"    could not parse reset: {e}")

save()
print(f"\n[requests used] {used}")
