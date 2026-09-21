"""Diagnose exactly why the Cloudflare solve fails on a CI runner.

Written because GitHub's job logs need an auth token to read, so the runner
writes its findings into the repo instead, where they can be read publicly.

Distinguishes the cases that need completely different responses:
  - Chrome never launched (environment problem, fixable)
  - the challenge is a timed one that just needs longer than we allowed
  - an interactive Turnstile widget that needs a click
  - a hard IP block ("Sorry, you have been blocked"), which is NOT a challenge
    and means datacentre IPs are refused outright
"""
import json
import os
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8")

OUT = "diagnostics"
os.makedirs(OUT, exist_ok=True)
report = {"when": time.strftime("%Y-%m-%dT%H:%M:%S")}


def save():
    with open(os.path.join(OUT, "ci_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


# --- what does a plain request see from this IP? ------------------------
try:
    import requests
    r = requests.get(
        "https://cults3d.com/en",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/140.0.0.0 Safari/537.36"},
        timeout=30)
    report["plain_requests"] = {
        "status": r.status_code,
        "cf_mitigated": r.headers.get("Cf-Mitigated"),
        "cf_ray": r.headers.get("CF-RAY"),
        "server": r.headers.get("Server"),
        "len": len(r.text),
        "title": (r.text.split("<title>")[1].split("</title>")[0]
                  if "<title>" in r.text else None),
        # A block page and a challenge page are different things.
        "looks_blocked": "have been blocked" in r.text or "Error 1006" in r.text,
        "looks_challenge": "Just a moment" in r.text,
        "body_snippet": r.text[:400],
    }
except Exception as e:
    report["plain_requests"] = {"error": f"{type(e).__name__}: {e}"}
save()

# --- can patchright drive real Chrome here at all? ----------------------
try:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir="ci_diag_profile",
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        report["chrome_launched"] = True
        report["ua"] = page.evaluate("navigator.userAgent")

        resp = page.goto("https://cults3d.com/en",
                         wait_until="domcontentloaded", timeout=60000)
        report["goto"] = {
            "status": resp.status if resp else None,
            "cf_mitigated": resp.header_value("cf-mitigated") if resp else None,
        }

        # Watch for 90s - triple what cleared it locally - and record the
        # whole trajectory, so "needs longer" is distinguishable from "stuck".
        timeline = []
        cleared_at = None
        for i in range(90):
            time.sleep(1)
            try:
                title = page.title()
                frames = sum(1 for f in page.frames
                             if "challenges.cloudflare.com" in f.url)
                body = page.evaluate(
                    "document.body ? document.body.innerText.slice(0,200) : ''")
            except Exception as e:
                timeline.append({"t": i + 1, "err": type(e).__name__})
                continue
            if i % 5 == 0:
                timeline.append({"t": i + 1, "title": title,
                                 "turnstile_frames": frames,
                                 "body": body[:120]})
            if "Just a moment" not in title and title:
                cleared_at = i + 1
                timeline.append({"t": i + 1, "title": title, "CLEARED": True})
                break
        report["timeline"] = timeline
        report["cleared_at"] = cleared_at
        report["cookies"] = sorted(c["name"] for c in ctx.cookies())
        report["has_cf_clearance"] = any(
            c["name"] == "cf_clearance" for c in ctx.cookies())
        try:
            report["final_body"] = page.evaluate("document.body.innerText")[:1500]
        except Exception:
            report["final_body"] = None
        page.screenshot(path=os.path.join(OUT, "ci_state.png"))
        ctx.close()
except Exception:
    report["chrome_launched"] = report.get("chrome_launched", False)
    report["chrome_error"] = traceback.format_exc()[-2000:]

save()
print(json.dumps({k: v for k, v in report.items()
                  if k not in ("timeline", "final_body")}, indent=2)[:3000])
print(f"\nwrote {OUT}/ci_report.json")
