"""Capture the browser's automation fingerprint. Makes NO network requests.

Purpose: the CI attempt at Cults3D failed with three variables changed at once
(datacentre IP, xvfb virtual display, Linux). This captures the signals a bot
detector actually reads, on `about:blank`, so the same script can be run on the
working machine and on a CI runner and the two diffed. Whatever differs is the
candidate cause - no guessing, and nothing is fetched from anyone.

Run:  python fingerprint.py out.json
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from patchright.sync_api import sync_playwright

PROBE = r"""
() => {
  const out = {};
  const safe = (label, fn) => { try { out[label] = fn(); } catch (e) { out[label] = "ERR:" + e.name; } };

  // The classic automation flag patchright is supposed to hide.
  safe("navigator.webdriver", () => navigator.webdriver);
  safe("has_window_chrome", () => typeof window.chrome !== "undefined");
  safe("chrome_runtime", () => !!(window.chrome && window.chrome.runtime));
  safe("plugins_length", () => navigator.plugins.length);
  safe("mimeTypes_length", () => navigator.mimeTypes.length);
  safe("languages", () => navigator.languages.join(","));
  safe("platform", () => navigator.platform);
  safe("hardwareConcurrency", () => navigator.hardwareConcurrency);
  safe("deviceMemory", () => navigator.deviceMemory);
  safe("maxTouchPoints", () => navigator.maxTouchPoints);
  safe("userAgent", () => navigator.userAgent);
  safe("uaData_platform", () => navigator.userAgentData && navigator.userAgentData.platform);

  // Headless/VM giveaway: a software rasteriser instead of a real GPU.
  safe("webgl_vendor", () => {
    const c = document.createElement("canvas").getContext("webgl");
    const d = c.getExtension("WEBGL_debug_renderer_info");
    return c.getParameter(d.UNMASKED_VENDOR_WEBGL);
  });
  safe("webgl_renderer", () => {
    const c = document.createElement("canvas").getContext("webgl");
    const d = c.getExtension("WEBGL_debug_renderer_info");
    return c.getParameter(d.UNMASKED_RENDERER_WEBGL);
  });

  // Window geometry: zeros and mismatches betray a virtual display.
  safe("screen", () => `${screen.width}x${screen.height}`);
  safe("avail", () => `${screen.availWidth}x${screen.availHeight}`);
  safe("colorDepth", () => screen.colorDepth);
  safe("outer", () => `${outerWidth}x${outerHeight}`);
  safe("inner", () => `${innerWidth}x${innerHeight}`);
  safe("devicePixelRatio", () => devicePixelRatio);

  safe("timezone", () => Intl.DateTimeFormat().resolvedOptions().timeZone);
  safe("notification_permission", () => Notification.permission);
  safe("pdfViewerEnabled", () => navigator.pdfViewerEnabled);
  safe("connection_type", () => navigator.connection && navigator.connection.effectiveType);

  // Fonts differ hugely between a desktop and a bare CI container.
  safe("font_count", () => {
    const probe = ["Arial","Verdana","Times New Roman","Courier New","Georgia",
                   "Tahoma","Trebuchet MS","Impact","Comic Sans MS","Segoe UI",
                   "Calibri","Cambria","Consolas","Helvetica","Roboto"];
    const base = "monospace";
    const s = document.createElement("span");
    s.style.fontSize = "72px"; s.textContent = "mmmmmmmmmmlli";
    document.body.appendChild(s);
    s.style.fontFamily = base;
    const w0 = s.offsetWidth;
    let n = 0;
    for (const f of probe) {
      s.style.fontFamily = `'${f}',${base}`;
      if (s.offsetWidth !== w0) n++;
    }
    s.remove();
    return n + "/" + probe.length;
  });

  // Error-stack shape is one way CDP presence leaks.
  safe("stack_has_puppeteer", () => {
    try { null.f(); } catch (e) {
      return /puppeteer|playwright|patchright|devtools/i.test(e.stack || "");
    }
    return false;
  });

  return out;
}
"""


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "fingerprint.json"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir="fp_profile",
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("about:blank")          # no network, deliberately
        fp = page.evaluate(PROBE)
        ctx.close()

    fp["_python"] = sys.version.split()[0]
    fp["_os"] = sys.platform
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(fp, f, indent=2, ensure_ascii=False)
    for k, v in fp.items():
        print(f"  {k:<26} = {v}")
    print(f"\n[saved] {dest}")


if __name__ == "__main__":
    main()
