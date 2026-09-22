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
cache hits on repeated URLs.

Pushing past 2 req/s does not help; it just wastes requests:

| target | achieved | 429s |
| --- | --- | --- |
| 2.5 req/s | 1.98 | 20% |
| 3.0 req/s | 2.25 | 25% |
| 3.3 req/s | 2.08 | 38% |

**~2 req/s is a hard ceiling per IP.**

### GitHub Actions does not work for this site

Verified on a real runner (`diagnostics/ci_report.json`): Chrome launches fine
under xvfb, the page loads, but **`cf_clearance` is never issued** — 90 seconds
of "Performing security verification" and an empty cookie jar, where the same
code on the author's home machine clears in 7 seconds.

This was then investigated properly instead of guessed at.

A network-free fingerprint probe (`fingerprint.py`, runs on `about:blank`) was
captured on both machines and diffed. The runner failed on two signals a
detector can act on alone:

| signal | working machine | CI runner |
| --- | --- | --- |
| `webgl_renderer` | `ANGLE (AMD Radeon 780M, D3D11)` | **threw `TypeError`** — no WebGL at all |
| `font_count` | 13/15 | 4/15 before fonts were installed |

`navigator.webdriver` was `false` on both, so patchright was doing its job —
the environment was the problem, not the stealth layer.

Both were then repaired: desktop font packages installed, and GPU flags added
so Chrome falls back to SwiftShader rather than having no WebGL
(`--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`). Fonts
reached 9/15, which is normal for Linux — the probe list is Windows-biased, so
9/15 is what a real Linux desktop scores.

**It still did not clear.** A single probe from a repaired runner
(`diagnostics/ci_challenge_probe.json`) sat on "Performing security
verification" for 60s with an empty cookie jar.

So CI is a dead end, and now on evidence rather than assumption. Two candidate
causes remain and were not separated further, because **neither is fixable on
free CI**: datacentre IP reputation, and the `SwiftShader` renderer string,
which itself marks a machine with no GPU. Note the response is
`Cf-Mitigated: challenge`, not a block — runner IPs are not banned, they simply
cannot pass.

This is the key difference from the sibling project: AWS WAF did not care where
the request came from, Cloudflare does. **The 20-runner fan-out is unavailable**,
and the crawl is limited to whatever residential IPs are on hand.

### What it actually does in production

Measured over consecutive one-hour windows of the real run, not a benchmark:

| | |
| --- | --- |
| sustained rate | **1.88 req/s** (~6,800 models/hour) |
| permanently gone (404/410) | **3.3%** of the index |
| transient failures | ~0.2% |
| full catalogue | **~18 days** of continuous running from one IP |

The gap between this and the 2.00 req/s benchmark is real work the benchmark
never did: deleted models, retries, and periodic challenge re-solves.

`--shard I/N` splits the index deterministically, so a second machine on a
different IP (a phone hotspot, not the same house) roughly halves the wall
clock.

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
