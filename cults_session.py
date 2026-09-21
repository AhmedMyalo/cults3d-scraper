"""Cults3D access layer.

Cloudflare facts established by probing (2026-09-22):
  - Every URL, robots.txt included, sits behind a Cloudflare managed challenge
    (403 + Cf-Mitigated: challenge).
  - Plain requests, and curl_cffi with Chrome TLS impersonation, both fail the
    challenge on their own. It genuinely requires JS execution.
  - Stock Playwright never clears it, headed or headless: the Turnstile iframe
    cycles forever because the automation is detected.
  - patchright + real Chrome + persistent profile clears it in ~7s, but ONLY
    headed. Headless still loops. On a Linux runner this means xvfb.
  - Once solved, cf_clearance transplants cleanly: plain requests gets HTTP 200.
    So the browser is needed once per session, not once per request.
"""
import json
import os
import time

import requests

PROFILE_DIR = os.environ.get("CULTS_PROFILE", "chrome_profile")
SESSION_FILE = os.environ.get("CULTS_SESSION", "session.json")
WARMUP_URL = "https://cults3d.com/en"


def solve_challenge(profile_dir=PROFILE_DIR, url=WARMUP_URL, timeout=60):
    """Clear the Cloudflare challenge in a real browser, return (ua, cookies)."""
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=False,          # headless is detected; do not change
            no_viewport=True,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(timeout):
                time.sleep(1)
                try:
                    if "Just a moment" not in page.title():
                        break
                except Exception:
                    pass
            else:
                raise RuntimeError("Cloudflare challenge not cleared within timeout")
            time.sleep(2)
            ua = page.evaluate("navigator.userAgent")
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if "cf_clearance" not in cookies:
                raise RuntimeError(f"no cf_clearance in cookies: {sorted(cookies)}")
            return ua, cookies
        finally:
            ctx.close()


def make_session(force=False):
    """Return a requests.Session carrying a valid cf_clearance.

    Reuses a cached session file when present; cf_clearance is bound to IP+UA,
    so the cache is only valid from the same machine.
    """
    data = None
    if not force and os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)

    if data is None:
        ua, cookies = solve_challenge()
        data = {"ua": ua, "cookies": cookies}
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    s = requests.Session()
    s.headers.update({
        "User-Agent": data["ua"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    s.cookies.update(data["cookies"])
    return s


def get(session, url, retries=3, **kw):
    """GET with re-solve on challenge. Returns the response or raises."""
    for attempt in range(retries):
        r = session.get(url, timeout=30, **kw)
        if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
            return r
        if r.headers.get("Cf-Mitigated") == "challenge" or r.status_code == 403:
            fresh = make_session(force=True)
            session.headers.update(fresh.headers)
            session.cookies.update(fresh.cookies)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        return r
    return r
