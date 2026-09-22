"""Parse a Cults3D model page into a flat row.

Field sources were confirmed against live pages on 2026-09-22:

  - Counters live in `<ul id="counters-creation-<id>">`, one <li> per metric.
    That element id is also the cleanest source of the design id.
  - `views` is ONLY ever rendered rounded ("4k"). There is no exact value in
    any title/data attribute. We keep the raw string AND a parsed integer that
    is explicitly approximate, so nobody mistakes 4000 for a real count.
    likes / downloads / collections / comments / makes are all exact.
  - price/currency come from LD+JSON, not the rendered price: the rendered one
    is localised to the scraping IP (we saw Romanian Lei), LD+JSON is explicit
    (EUR) and therefore comparable across rows.
  - The model's own categories are the breadcrumb inside the
    `creation-page__tab-section` whose heading is "Categories". Every other
    /categories/ link on the page is nav chrome and must not be picked up.
"""
import json
import re

from bs4 import BeautifulSoup

# Cults3D renders the singular when a counter is exactly 1 ("1 like", not
# "1 likes"). Matching only the plural silently nulls every count of 1, which
# is a large share of the long tail - so the trailing 's' must be optional.
COUNTER_RE = re.compile(
    r"([\d.,]+\s*[kKmM]?)\s+(view|like|download|collection|comment|make)s?\b")
ID_RE = re.compile(r"counters-creation-(\d+)")


def _num(text):
    """'4k' -> 4000, '1.2M' -> 1200000, '147' -> 147, '' -> None."""
    if not text:
        return None
    t = text.strip().replace(",", "").replace(" ", "").replace(" ", "")
    m = re.match(r"^([\d.]+)([kKmM]?)$", t)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2).lower()
    if suf == "k":
        val *= 1_000
    elif suf == "m":
        val *= 1_000_000
    return int(val)


def _ld_json(soup):
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("@type") == "MediaObject":
            return d
    return {}


def parse_model(html, url=None):
    soup = BeautifulSoup(html, "html.parser")
    ld = _ld_json(soup)
    row = {"url": url}

    # --- identity -------------------------------------------------------
    counters = soup.select_one('ul[id^="counters-creation-"]')
    design_id = None
    if counters:
        m = ID_RE.search(counters.get("id", ""))
        if m:
            design_id = int(m.group(1))
    if design_id is None:
        m = re.search(r"/:(\d+)", ld.get("identifier", "") or "")
        if m:
            design_id = int(m.group(1))
    row["design_id"] = design_id
    if url:
        mu = re.match(r"https://cults3d\.com/en/3d-model/([^/]+)/([^/?#]+)", url)
        if mu:
            row["group"] = mu.group(1)
            row["slug"] = mu.group(2)

    row["title"] = ld.get("name")
    row["description"] = ld.get("description")
    row["published_at"] = ld.get("datePublished")
    row["license_url"] = ld.get("license")
    row["file_format"] = ld.get("encodingFormat")
    row["image"] = ld.get("image")

    # --- engagement (the priority fields) -------------------------------
    for k in ("views", "likes", "downloads", "collections", "comments", "makes"):
        row[k] = None
    row["views_raw"] = None
    if counters:
        for li in counters.select("li"):
            m = COUNTER_RE.search(li.get_text(" ", strip=True))
            if not m:
                continue
            raw, kind = m.group(1).strip(), m.group(2) + "s"
            if kind == "views":
                # Rounded at source; keep the string so the loss is visible.
                row["views_raw"] = raw
                row["views"] = _num(raw)
                row["views_is_approx"] = bool(re.search(r"[kKmM]", raw))
            else:
                row[kind] = _num(raw)
    row.setdefault("views_is_approx", None)

    # --- rating ---------------------------------------------------------
    rating = ld.get("aggregateRating") or {}
    row["rating_value"] = rating.get("ratingValue")
    row["rating_count"] = rating.get("reviewCount")

    # --- price ----------------------------------------------------------
    offers = ld.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    if offers:
        row["price"] = offers[0].get("price")
        row["currency"] = offers[0].get("priceCurrency")
        row["is_free"] = (offers[0].get("description") == "free"
                          or offers[0].get("price") in (0, 0.0))
    else:
        row["price"] = row["currency"] = row["is_free"] = None

    # --- author ---------------------------------------------------------
    creator = ld.get("creator") or {}
    row["author"] = creator.get("name")
    row["author_url"] = creator.get("url")

    # --- the tabbed panels, keyed by their heading -----------------------
    # Headings seen: "3D model description", "3D printing settings",
    # "Categories", "Tags", plus a Cults promo block that must be ignored.
    sections = {}
    for sec in soup.select("div.creation-page__tab-section"):
        h = sec.find(["h2", "h3"])
        if h:
            sections[h.get_text(" ", strip=True).strip().lower()] = sec

    def section_body(sec, heading):
        """Section text with its own heading removed."""
        txt = sec.get_text("\n", strip=True)
        if txt.lower().startswith(heading.lower()):
            txt = txt[len(heading):].strip()
        return txt or None

    settings = sections.get("3d printing settings")
    row["printing_settings"] = (section_body(settings, "3D printing settings")
                                if settings else None)

    # Cults machine-translates descriptions; a translated page carries a
    # notice and a link to the original. Worth flagging, because it means the
    # description text is not necessarily the designer's own words.
    row["is_auto_translated"] = bool(
        re.search(r"translated by automatic translation", html, re.I))

    # --- categories (breadcrumb only) + tags -----------------------------
    cats = []
    catsec = sections.get("categories")
    if catsec:
        for a in catsec.select('a[href*="/categories/"]'):
            slug = a["href"].rstrip("/").rsplit("/", 1)[-1]
            if slug != "categories":
                cats.append(slug)
    row["categories"] = cats

    row["tags"] = [a["href"].rstrip("/").rsplit("/", 1)[-1]
                   for a in soup.select('a[href*="/tags/"]')]

    # --- spec table (License / Usages / 3D design format / ...) ----------
    # Rendered as <tr><th>label</th><td>value</td></tr>. Read the labels
    # rather than positions, so a re-ordered table does not silently shift
    # every field by one.
    spec = {}
    for tr in soup.select("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            spec[th.get_text(" ", strip=True).strip().lower()] = td

    lic = spec.get("license")
    # The cell text interleaves icon glyphs ("👤 CULTS PU 🚫 AI No AI"), so
    # take the link labels instead of the raw text.
    row["license"] = ([s.get_text(" ", strip=True)
                       for s in lic.select("span.link--strong")] if lic else [])
    row["is_no_ai"] = any("no ai" in l.lower() for l in row["license"])

    usages = spec.get("usages")
    row["usages"] = ([a.get_text(" ", strip=True) for a in usages.select("a")]
                     if usages else [])

    fmt = spec.get("3d design format")
    row["file_count"] = None
    row["file_names"] = []
    if fmt:
        summary = fmt.find("summary")
        label = summary.get_text(" ", strip=True) if summary else ""
        # The format in parentheses is optional: plenty of models render just
        # "2 files". Requiring the parentheses dropped the count entirely on
        # ~7% of models.
        m = re.search(r"(\d+)\s*files?(?:\s*\(([^)]*)\))?", label)
        if m:
            row["file_count"] = int(m.group(1))
            if m.group(2):
                row["file_format"] = m.group(2).strip()  # beats LD+JSON
        # Each <li> is "<span>name</span><span>23.0 x 53.5 mm</span>"; take
        # only the first span or every filename carries its dimensions.
        names = []
        for li in fmt.select("ul.list--bullet li"):
            first = li.find("span")
            names.append((first or li).get_text(" ", strip=True))
        row["file_names"] = names
        # Fall back to the actual extensions when neither the label nor
        # LD+JSON gave a format.
        if not row.get("file_format") and names:
            exts = sorted({n.rsplit(".", 1)[-1].upper()
                           for n in names if "." in n})
            row["file_format"] = " and ".join(exts) if exts else None

    # --- designer standing ----------------------------------------------
    # Both are useful market signals: whether Cults has verified the account,
    # and the seller tier badge (seller_1..N) it awards established sellers.
    row["author_is_certified"] = bool(
        soup.select_one('a[href*="certified-account"]'))
    badge = soup.select_one('img[src*="badges/seller"]')
    row["author_seller_badge"] = None
    if badge:
        m = re.search(r"seller_(\d+)", badge.get("src", ""))
        row["author_seller_badge"] = int(m.group(1)) if m else None

    # --- designer's own totals ------------------------------------------
    for k in ("author_designs", "author_downloads", "author_followers",
              "author_sales"):
        row[k] = None
    stats = soup.select_one("ul.list--statistics")
    if stats:
        for li in stats.select("li"):
            cnt = li.select_one("span.list--statistics__count")
            if not cnt:
                continue
            value = _num(cnt.get_text(strip=True))
            label = li.get_text(" ", strip=True).lower()
            if "design" in label:
                row["author_designs"] = value
            elif "download" in label:
                row["author_downloads"] = value
            elif "follower" in label:
                row["author_followers"] = value
            elif "sale" in label:
                row["author_sales"] = value

    return row
