"""Merge the three collections into one dataset, deduped by slug.

The three were gathered by different means and do not share a schema:

  details/   49,939  scraped from the website. Rich on the designer (sales,
                     followers, seller badge, certification) and on files, but
                     `views` is the rounded string the page renders ("1k"), so
                     the number is not real.
  api_data/  26,571  official API, oldest-first sweep.
  top_data/ 261,882  official API, top-downloaded per category.

Where a model appears in both, the API row wins on engagement because its
viewsCount is exact where the scraped one is rounded - the same design showed
"1k" scraped and 382,574 through the API. The scraped row is still kept for the
designer and file columns the API does not expose, so nothing is thrown away.
"""
import csv
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

OUT = "merged"
COLUMNS = [
    "slug", "design_id", "identifier", "title", "url",
    "author", "author_url",
    # Engagement. views_exact is from the API; views_approx only exists for
    # rows we never saw through the API, and is rounded at source.
    "views_exact", "views_approx", "views_is_approx",
    "downloads", "likes", "makes_count", "comments_count", "collections_count",
    "rating_value", "rating_count",
    "price", "currency", "is_free", "total_sales_amount",
    "category", "source_category", "categories", "tags", "tags_count",
    "published_at", "updated_at",
    "file_count", "file_format", "license", "made_with_ai", "featured",
    "author_designs", "author_downloads", "author_followers",
    "author_sales", "author_sales_currency", "author_seller_badge",
    "author_is_certified",
    "printing_settings", "description", "image",
    "source",
]


def rows(pattern):
    for p in sorted(glob.glob(pattern)):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def flat(v, key=None):
    """Money/creator/category come back as small objects; take the useful bit."""
    if isinstance(v, dict):
        if key:
            return v.get(key)
        for k in ("value", "cents", "name", "nick", "slug"):
            if k in v:
                return v[k]
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "|".join(
            str(x.get("name") or x.get("slug") or x.get("nick") or x)
            if isinstance(x, dict) else str(x) for x in v)
    return v


def from_api(r, source):
    price = r.get("price") or {}
    return {
        "slug": r.get("slug"),
        "identifier": r.get("identifier"),
        "title": r.get("name"),
        "url": r.get("url"),
        "author": flat(r.get("creator"), "nick"),
        "views_exact": r.get("viewsCount"),
        "downloads": r.get("downloadsCount"),
        "likes": r.get("likesCount"),
        "makes_count": r.get("makes_count"),
        "comments_count": r.get("comments_count"),
        "collections_count": r.get("collections_count"),
        "price": price.get("value", price.get("cents")),
        "total_sales_amount": flat(r.get("totalSalesAmount")),
        "category": flat(r.get("category"), "name"),
        "source_category": r.get("source_category"),
        "categories": flat(r.get("subCategories")),
        "tags": flat(r.get("tags")),
        "tags_count": r.get("tags_count"),
        "published_at": r.get("publishedAt"),
        "updated_at": r.get("updatedAt"),
        "license": flat(r.get("license")),
        "made_with_ai": r.get("madeWithAi"),
        "featured": r.get("featured"),
        "description": r.get("description"),
        "image": r.get("illustrationImageUrl"),
        "source": source,
    }


def from_scrape(r):
    return {
        "slug": r.get("slug"),
        "design_id": r.get("design_id"),
        "title": r.get("title"),
        "url": r.get("url"),
        "author": r.get("author"),
        "author_url": r.get("author_url"),
        "views_approx": r.get("views"),
        "views_is_approx": r.get("views_is_approx"),
        "downloads": r.get("downloads"),
        "likes": r.get("likes"),
        "makes_count": r.get("makes"),
        "comments_count": r.get("comments"),
        "collections_count": r.get("collections"),
        "rating_value": r.get("rating_value"),
        "rating_count": r.get("rating_count"),
        "price": r.get("price"),
        "currency": r.get("currency"),
        "is_free": r.get("is_free"),
        "categories": flat(r.get("categories")),
        "tags": flat(r.get("tags")),
        "published_at": r.get("published_at"),
        "file_count": r.get("file_count"),
        "file_format": r.get("file_format"),
        "license": flat(r.get("license")),
        "author_designs": r.get("author_designs"),
        "author_downloads": r.get("author_downloads"),
        "author_followers": r.get("author_followers"),
        "author_sales": r.get("author_sales"),
        "author_sales_currency": r.get("author_sales_currency"),
        "author_seller_badge": r.get("author_seller_badge"),
        "author_is_certified": r.get("author_is_certified"),
        "printing_settings": r.get("printing_settings"),
        "description": r.get("description"),
        "image": r.get("image"),
        "source": "scraped",
    }


def main():
    merged, stats = {}, {"scraped": 0, "api_sweep": 0, "api_top": 0,
                         "enriched": 0}

    # Scraped first so the API passes can overwrite its engagement numbers.
    for r in rows("details/part_*.jsonl"):
        if r.get("slug"):
            merged[r["slug"]] = from_scrape(r)
            stats["scraped"] += 1

    for pattern, label in (("api_data/part_*.jsonl", "api_sweep"),
                           ("top_data/part_*.jsonl", "api_top")):
        for r in rows(pattern):
            slug = r.get("slug")
            if not slug:
                continue
            stats[label] += 1
            new = from_api(r, label)
            old = merged.get(slug)
            if old:
                # Keep the scraped-only columns, let the API win everywhere
                # else - its view count is real, the scraped one is rounded.
                stats["enriched"] += 1
                for k, v in new.items():
                    if v is not None:
                        old[k] = v
                old["source"] = f"scraped+{label}"
            else:
                merged[slug] = new

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "cults3d_merged.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in merged.values():
            w.writerow(row)

    print(f"read     : {stats['scraped']:,} scraped | "
          f"{stats['api_sweep']:,} api sweep | {stats['api_top']:,} api top")
    print(f"           {sum(stats[k] for k in ('scraped','api_sweep','api_top')):,} rows in")
    print(f"overlap  : {stats['enriched']:,} models seen by more than one pass")
    print(f"unique   : {len(merged):,} models out")
    exact = sum(1 for r in merged.values() if r.get("views_exact") is not None)
    print(f"\nexact view counts : {exact:,} ({100*exact/len(merged):.0f}%)")
    print(f"rounded only      : {len(merged)-exact:,}")
    print(f"\n[saved] {path}")


if __name__ == "__main__":
    main()
