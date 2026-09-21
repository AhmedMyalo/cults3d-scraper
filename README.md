# cults3d-scraper

Full-catalogue scraper for [cults3d.com](https://cults3d.com): every model, from
every category, with per-model detail. Runs free and unattended on GitHub
Actions. Sibling project to
[cgtrader-scraper](https://github.com/AhmedMyalo/cgtrader-scraper).

## What was learned (measured, not assumed)

### Cloudflare

Every URL, `robots.txt` included, sits behind a Cloudflare **managed
challenge** (`403` + `Cf-Mitigated: challenge`).

| approach | result |
| --- | --- |
| plain `requests` | 403 |
| `curl_cffi` TLS impersonation (chrome/safari/firefox) | 403 — fingerprint alone is not enough |
| stock Playwright, headed, 45s | never clears; the Turnstile iframe cycles = automation detected |
| **patchright + real Chrome, headed** | **clears in ~7s, yields `cf_clearance`** |
| patchright headless | still fails |

The challenge is a spinner, not a checkbox, so it is not waiting on a click.

Once solved, **`cf_clearance` transplants into a plain `requests.Session`** and
returns HTTP 200, so the browser is needed once per IP, not once per request.

Two consequences for CI: Chrome must run under **`xvfb`** (headless is
detected), and `cf_clearance` is bound to IP + User-Agent, so each runner
solves its own.

### Rate limits — the real constraint

Sustained 100-second windows against one IP:

| config | rate | 429s |
| --- | --- | --- |
| 1 worker, unpaced | 1.75 req/s | 0% |
| 2 workers, unpaced | 2.14 req/s | 78% |
| 4 workers, 0.25s gap | 2.17 req/s | 44% |
| **4 workers, 0.5s gap** (default) | **2.00 req/s** | **0%** |

A single IP sustains **~2 req/s** regardless of thread count. Short bursts hit
15 req/s, which is misleading — that is a leaky bucket refilling, plus CDN
cache hits on repeated URLs. **Throughput comes from more IPs (runners), never
from more threads.**

### Catalogue enumeration

`https://cults3d.com/sitemap.xml` indexes 3,206 child sitemaps. The 403 English
`creationsN.xml.gz` files hold 10,000 URLs each, ~78% of them model pages:
**~3.15M models enumerable in ~403 gzipped requests.**

This removes the need for a category-pagination crawl entirely, and with it the
per-listing row cap, the unstable default sort, and the "NO AI" default filter —
none of which can affect a sitemap.

## Known data limitations

- **`views` is only ever rendered rounded** (`"4k"`). There is no exact value
  anywhere in the DOM. `views_raw` keeps the original string and
  `views_is_approx` flags it. **`downloads` is exact** and is the better
  engagement measure here.
- **`price` is in the seller's own currency** (EUR/USD/GBP/CAD/…), taken from
  LD+JSON. The rendered price is localised to the scraping IP and is not
  comparable across rows; comparing prices needs FX conversion.
- Comments and makes are captured as **counts only**, by decision — not their
  contents.

## Usage

```bash
pip install -r requirements.txt

python build_url_index.py                 # ~403 requests -> url_index/
python cults_detail_scrape.py             # resumable; --shard I/N to parallelise
python check_completion.py                # coverage vs. the site's own totals
python export_csv.py                      # JSONL shards -> csv/, deduped by id
```

On Linux the scraper must be wrapped: `xvfb-run -a python cults_detail_scrape.py`.

Run the `scrape` workflow with `shards: 20` to fan out across 20 runner IPs.

## Layout

| file | role |
| --- | --- |
| `cults_session.py` | Cloudflare solve + `cf_clearance` transplant |
| `build_url_index.py` | sitemaps → master model URL list |
| `cults_parse.py` | model page → flat row |
| `cults_detail_scrape.py` | sharded, resumable, adaptively throttled fetch loop |
| `check_completion.py` | coverage + field-fill-rate report |
| `commit_progress.sh` | chunked pushes with union-merge conflict resolution |
