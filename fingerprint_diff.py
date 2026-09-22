"""Diff the Windows baseline fingerprint against the CI one.

Flags each difference by how much a bot detector is likely to care, so the
output points at a cause instead of listing noise. Nothing is fetched.
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

# How damning each signal is when it looks wrong. "high" means a detector can
# treat it alone as strong evidence of an automated/virtual environment.
WEIGHT = {
    "navigator.webdriver": "high",
    "webgl_renderer": "high",
    "webgl_vendor": "high",
    "font_count": "high",
    "stack_has_puppeteer": "high",
    "has_window_chrome": "high",
    "plugins_length": "medium",
    "mimeTypes_length": "medium",
    "hardwareConcurrency": "medium",
    "deviceMemory": "medium",
    "outer": "medium",
    "devicePixelRatio": "medium",
    "pdfViewerEnabled": "medium",
    "notification_permission": "medium",
    # Expected to differ between a Windows desktop and a Linux runner, and
    # not suspicious on their own.
    "platform": "expected",
    "userAgent": "expected",
    "uaData_platform": "expected",
    "timezone": "expected",
    "_os": "expected",
    "_python": "expected",
    "screen": "expected",
    "avail": "expected",
    "inner": "expected",
    "colorDepth": "expected",
    "languages": "expected",
    "connection_type": "expected",
    "maxTouchPoints": "expected",
}

a_path = sys.argv[1] if len(sys.argv) > 1 else "fp_windows.json"
b_path = sys.argv[2] if len(sys.argv) > 2 else "diagnostics/fp_ci_xvfb.json"

a = json.load(open(a_path, encoding="utf-8"))
b = json.load(open(b_path, encoding="utf-8"))

buckets = {"high": [], "medium": [], "expected": [], "same": []}
for k in sorted(set(a) | set(b)):
    va, vb = a.get(k, "<missing>"), b.get(k, "<missing>")
    if va == vb:
        buckets["same"].append((k, va))
    else:
        buckets[WEIGHT.get(k, "medium")].append((k, va, vb))

print(f"WINDOWS (works)   : {a_path}")
print(f"CI xvfb (failed)  : {b_path}\n")

for label, title in (("high", "SUSPICIOUS - a detector can act on these alone"),
                     ("medium", "NOTABLE - contributes to a bot score"),
                     ("expected", "expected platform differences, not suspicious")):
    rows = buckets[label]
    print(f"=== {title} ({len(rows)}) ===")
    if not rows:
        print("   (none)")
    for k, va, vb in rows:
        print(f"   {k}")
        print(f"      windows : {va}")
        print(f"      ci      : {vb}")
    print()

print(f"=== identical on both ({len(buckets['same'])}) ===")
print("   " + ", ".join(k for k, _ in buckets["same"]))

n_high = len(buckets["high"])
print()
if n_high == 0:
    print("VERDICT: no high-weight difference. The browser environment does NOT")
    print("         look obviously automated, which points at IP reputation")
    print("         rather than fingerprinting - CI would stay a dead end.")
else:
    print(f"VERDICT: {n_high} high-weight difference(s). The CI browser is")
    print("         distinguishable on signals detectors actually use, so")
    print("         'datacentre IP' was not established as the cause.")
