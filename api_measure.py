"""Measure the three numbers the API plan depends on, without provoking anything.

  1. what fields a Creation actually exposes  (does it match what we scraped?)
  2. the real rate limit                      (read from response headers, NOT
                                               by hammering until it returns 429)
  3. how deep offset pagination goes          (the CGTrader row-cap problem)

Deliberately does not try to find the daily limit by exhausting it. Ahmed's
standing rule is that the first 429 ends the run and I ask him, so provoking
one on purpose would be the wrong shape even though this is an authenticated
API. Headers first; if they say nothing, I report that rather than guess.

    CULTS_USER=<nick> CULTS_KEY=<key> python api_measure.py
"""
import json
import os
import sys
import time

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
findings = {}


def gql(query, variables=None, label=""):
    global used
    used += 1
    r = s.post(ENDPOINT, json={"query": query, "variables": variables or {}},
               timeout=60)
    if r.status_code == 429:
        print(f"\n[STOP] 429 after {used} requests - stopping, per the rule")
        findings["hit_429_at_request"] = used
        save()
        sys.exit(0)
    if r.status_code == 403:
        print(f"\n[STOP] 403 after {used} requests")
        save()
        sys.exit(0)
    return r


def save():
    os.makedirs("diagnostics", exist_ok=True)
    findings["requests_used"] = used
    with open("diagnostics/api_measurements.json", "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2, ensure_ascii=False)


# --- 1. what does a Creation carry? -----------------------------------
print("[1] fields available on a Creation")
TYPE_Q = """
{ __type(name: "Creation") { fields { name
    type { name kind ofType { name kind ofType { name } } } } } }
"""
r = gql(TYPE_Q, label="creation-type")
data = r.json().get("data", {}).get("__type") or {}
fields = data.get("fields") or []


def tname(t):
    while t and not t.get("name"):
        t = t.get("ofType")
    return (t or {}).get("name", "?")


names = sorted((f["name"], tname(f["type"])) for f in fields)
findings["creation_fields"] = {n: t for n, t in names}
print(f"    {len(names)} fields:")
for n, t in names:
    print(f"      {n:<28} {t}")

# Do the engagement fields we care about exist?
want = ["viewsCount", "views", "likesCount", "downloadsCount", "commentsCount",
        "makesCount", "collectionsCount", "price", "name", "slug", "tags",
        "categories", "creator", "publishedAt", "createdAt", "illustrations",
        "license", "identifier", "id", "description", "totalDownloads"]
have = {n for n, _ in names}
print("\n    engagement / key fields:")
for w in want:
    print(f"      {'YES' if w in have else ' - ':<4} {w}")

# --- 2. what do the headers say about limits? -------------------------
print("\n[2] rate-limit headers on a normal request")
PING = '{ creationsBatch(limit: 1) { total results { id } } }'
r = gql(PING, label="headers")
hdrs = {k: v for k, v in r.headers.items()
        if any(w in k.lower() for w in
               ("ratelimit", "rate-limit", "retry", "quota", "limit"))}
findings["rate_limit_headers"] = hdrs
print(f"    {hdrs if hdrs else 'none exposed - the daily cap stays unverified'}")

body = r.json()
total = ((body.get("data") or {}).get("creationsBatch") or {}).get("total")
findings["reported_total_creations"] = total
print(f"    site reports total creations = {total:,}" if total else "    no total")

# --- 3. how deep can offset go? ---------------------------------------
print("\n[3] offset depth (the CGTrader row-cap question)")
DEEP = """
query($off: Int!) { creationsBatch(limit: 1, offset: $off, sort: BY_PUBLISHED_AT,
       direction: ASC) { total results { id slug } } }
"""
depth = {}
for off in (0, 10_000, 100_000, 500_000, 1_000_000, 2_500_000):
    r = gql(DEEP, {"off": off}, label=f"offset={off}")
    b = r.json()
    if b.get("errors"):
        msg = b["errors"][0].get("message", "")[:90]
        print(f"    offset={off:>9,} -> ERROR: {msg}")
        depth[off] = f"error: {msg}"
        break
    res = ((b.get("data") or {}).get("creationsBatch") or {}).get("results")
    ok = bool(res)
    print(f"    offset={off:>9,} -> {'returned a row' if ok else 'EMPTY'}")
    depth[off] = "ok" if ok else "empty"
    if not ok:
        break
    time.sleep(1)
findings["offset_depth"] = depth

save()
print(f"\n[requests used] {used}")
print("[saved] diagnostics/api_measurements.json")
