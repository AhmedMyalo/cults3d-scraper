"""ONE request to cults3d.com, to answer one question: can a CI runner clear
the Cloudflare challenge now that the browser fingerprint has been repaired?

Deliberately minimal and self-limiting:
  - loads exactly one URL, the site's own homepage
  - fetches no model pages and collects no data
  - stops the moment it reaches a verdict, including on a block page

Written after the earlier CI attempt was misdiagnosed: that run changed the IP,
the display and the OS at once, and "datacentre IP" was recorded as the cause
without evidence. This isolates the remaining question.
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from patchright.sync_api import sync_playwright

URL = "https://cults3d.com/en"
WAIT = 60
OUT = "diagnostics/ci_challenge_probe.json"

GL = ("--use-gl=angle --use-angle=swiftshader "
      "--enable-unsafe-swiftshader --ignore-gpu-blocklist").split()

report = {"url": URL, "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "requests_made": 1}

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="probe_profile",
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=GL,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    report["initial_status"] = resp.status if resp else None
    report["initial_cf_mitigated"] = resp.header_value("cf-mitigated") if resp else None

    verdict, cleared_at = None, None
    for i in range(WAIT):
        time.sleep(1)
        try:
            title = page.title()
            body = page.evaluate(
                "document.body ? document.body.innerText.slice(0,300) : ''")
        except Exception:
            continue
        low = (title + " " + body).lower()

        if "access to cults has been denied" in low or "access denied" in low:
            verdict = "BLOCKED"          # CrowdSec reaches datacentre IPs too
            break
        if "just a moment" not in low and "performing security verification" not in low:
            if title.strip():
                verdict, cleared_at = "CLEARED", i + 1
                break

    report["verdict"] = verdict or "NEVER_CLEARED"
    report["cleared_after_s"] = cleared_at
    try:
        report["final_title"] = page.title()
        report["final_body"] = page.evaluate("document.body.innerText")[:400]
    except Exception:
        pass
    cookies = ctx.cookies()
    report["cookies"] = sorted(c["name"] for c in cookies)
    report["has_cf_clearance"] = any(c["name"] == "cf_clearance" for c in cookies)
    ctx.close()

os.makedirs("diagnostics", exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)

print(json.dumps({k: v for k, v in report.items() if k != "final_body"}, indent=2))
print(f"\n[saved] {OUT}")
