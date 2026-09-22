"""Count how many models each filter combination actually returns.

The point is to replace my estimates with measured numbers before committing to
a collection plan. I guessed wrong three times in this project already, so the
percentages I quoted for "recent" and "paid" are worth nothing until counted.

Uses 2 requests per category (~200 total), leaving most of the daily 500 free.

    CULTS_USER=<nick> CULTS_KEY=<key> python api_counts.py --years 3
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from api_scrape import Api, Stop


def count(api, **flt):
    """One count query. Only `total` is fetched, so the response stays tiny."""
    args, variables, types = [], {}, []
    for k, v in flt.items():
        if v is None:
            continue
        gql_t = {"submittedAfter": "ISO8601DateTime",
                 "categorySlugEn": "String",
                 "onlyPriced": "Boolean"}[k]
        args.append(f"{k}: ${k}")
        types.append(f"${k}: {gql_t}")
        variables[k] = v
    q = (f"query({', '.join(types)}) {{ creationsBatch(limit: 1, "
         f"{', '.join(args)}) {{ total }} }}")
    return api(q, variables)["creationsBatch"]["total"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--out", default="diagnostics/category_counts.json")
    args = ap.parse_args()

    user, key = os.environ.get("CULTS_USER"), os.environ.get("CULTS_KEY")
    if not user or not key:
        sys.exit("CULTS_USER / CULTS_KEY not set")
    api = Api(user, key)

    since = (datetime.now(timezone.utc)
             - timedelta(days=365 * args.years)).replace(microsecond=0)
    print(f"counting models published since {since:%Y-%m-%d}\n")

    rows = []
    try:
        grand_all = count(api, submittedAfter=since.isoformat())
        grand_paid = count(api, submittedAfter=since.isoformat(), onlyPriced=True)
        print(f"WHOLE SITE, last {args.years}y : {grand_all:>9,} total | "
              f"{grand_paid:>9,} paid\n")

        cats = api("{ categories(safe: false) { slug name(locale: EN) } }")["categories"]
        print(f"{len(cats)} categories\n")
        print(f"{'category':<30}{'last '+str(args.years)+'y':>12}{'paid':>10}{'paid %':>8}")
        print("-" * 60)

        for c in cats:
            slug = c["slug"]
            n_all = count(api, submittedAfter=since.isoformat(), categorySlugEn=slug)
            n_paid = count(api, submittedAfter=since.isoformat(),
                           categorySlugEn=slug, onlyPriced=True)
            pct = (100 * n_paid / n_all) if n_all else 0
            rows.append({"slug": slug, "name": c.get("name"),
                         "recent": n_all, "recent_paid": n_paid})
            print(f"{slug[:29]:<30}{n_all:>12,}{n_paid:>10,}{pct:>7.0f}%")
    except Stop as e:
        print(f"\n[STOPPED] {e}")
    finally:
        os.makedirs("diagnostics", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"years": args.years, "since": since.isoformat(),
                       "categories": rows, "requests_used": api.count}, f,
                      indent=2, ensure_ascii=False)
        tot = sum(r["recent"] for r in rows)
        paid = sum(r["recent_paid"] for r in rows)
        print(f"\n[summary] {len(rows)} categories counted, {api.count} requests")
        print(f"   sum across categories : {tot:,} recent | {paid:,} paid")
        print(f"   (a model in several categories is counted more than once)")
        for label, n in (("all recent", tot), ("recent + paid only", paid)):
            reqs = -(-n // 100)
            print(f"   {label:<20} -> {reqs:>7,} requests -> "
                  f"{reqs/500:>5.1f} days at 500/day")


if __name__ == "__main__":
    main()
