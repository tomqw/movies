#!/usr/bin/env python3
"""Generate a self-contained catalog page (index.html) from IMDb links in movies.md.

Usage: python3 build.py [--list FILE] [--out FILE]

Standard library only, no pip installs.
Entry lines in movies.md are normalized by the build to
  - [Title (Year)](imdb-url) — personal note
where the bracketed label is refreshed from cached metadata on every run
and the text after the em-dash belongs to the user.
Data resolution per title:
  1. imdb.com title page JSON-LD when reachable (richest)
  2. otherwise IMDb suggestion endpoint for verified identity
     (title, year, poster, type, stars), enriched from Stremio's public
     Cinemeta CDN (rating, plot, genres, runtime, cast) - Cinemeta data is
     only accepted when its title matches the verified one.
Records with source "basic" are retried against both on later runs;
entries never fetched fall back to their markdown label.
All results are cached in data/cache.json.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_LIST = ROOT / "movies.md"
DEFAULT_OUT = ROOT / "index.html"
CACHE_FILE = ROOT / "data" / "cache.json"
PAGE_TITLE = "Movies & TV Shows"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
RETRY_DELAY = 2
FETCH_DELAY = 0.6

LINK_RE = re.compile(r"https?://(?:www\.)?imdb\.com/title/(tt\d{7,8})/?")
BARE_ID_RE = re.compile(r"^(tt\d{7,8})(?![0-9])")
LABEL_URL_RE = re.compile(
    r"\[([^\]]*)\]\(\s*(https?://(?:www\.)?imdb\.com/title/(tt\d{7,8}))[^)]*\)"
)
MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
LABEL_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)$")
LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
SUGGESTION_URL = (
    "https://v2.sg.media-imdb.com/suggestion/{letter}/{tt_id}.json"
)
CINEMETA_URL = "https://v3-cinemeta.strem.io/meta/{kind}/{tt_id}.json"

TYPE_LABELS = {
    "movie": "Movie",
    "tvSeries": "Series",
    "tvMiniSeries": "Mini Series",
    "tvMovie": "TV Movie",
    "videoGame": "Video Game",
    "short": "Short",
    "video": "Video",
}
SERIES_TYPES = {"tvSeries", "tvMiniSeries"}

LD_TYPE_LABELS = {
    "Movie": "Movie",
    "TVSeries": "Series",
    "TVMiniSeries": "Mini Series",
    "TVMovie": "TV Movie",
    "VideoGame": "Video Game",
}


def parse_line(raw: str) -> dict | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return None
    body = MARKER_RE.sub("", stripped, count=1)
    m = LABEL_URL_RE.search(body)
    if m:
        return {"id": m.group(3), "label": m.group(1).strip(), "after": body[m.end():]}
    m = LINK_RE.search(body)
    if m:
        return {"id": m.group(1), "label": "", "after": body[m.end():]}
    m = BARE_ID_RE.match(body)
    if m:
        return {"id": m.group(1), "label": "", "after": body[m.end():]}
    return None


def parse_markdown(path: Path):
    entries, seen = [], set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        info = parse_line(raw)
        if not info or info["id"] in seen:
            continue
        seen.add(info["id"])
        note = info["after"].strip().lstrip("—–-–").strip()
        if set(note) <= {"/"}:
            note = ""
        entries.append({"id": info["id"], "note": note, "label": info["label"], "line": line_no})
    return entries


def canonical_entry_line(entry: dict, record: dict | None) -> str | None:
    title = (record or {}).get("title") or entry["label"]
    if not title:
        return None
    year = (record or {}).get("year")
    label = f"{title} ({year})" if year else str(title)
    line = f"- [{label}](https://www.imdb.com/title/{entry['id']}/)"
    if entry["note"]:
        line += f" — {entry['note']}"
    return line


def annotate_markdown(path: Path, entries, cache) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    first_by_id = {}
    for entry in entries:
        first_by_id.setdefault(entry["id"], entry)
    changed = False
    for i, raw in enumerate(lines):
        info = parse_line(raw)
        if not info:
            continue
        entry = first_by_id.get(info["id"])
        if entry is None or entry["line"] != i:
            continue
        new = canonical_entry_line(entry, cache.get(info["id"]))
        if new is not None and new != raw:
            lines[i] = new
            changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def record_from_label(entry: dict) -> dict | None:
    label = entry.get("label") or ""
    title, year = label, None
    m = LABEL_YEAR_RE.match(label)
    if m:
        title, year = m.group(1).strip(), int(m.group(2))
    if not title:
        return None
    record = base_record(entry["id"])
    record.update(title=title, year=year, source="label")
    return record


def http_get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def extract_ldjson(html: str) -> dict | None:
    for block in LDJSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "@type" in data:
            return data
    return None


def parse_iso_duration(value: str) -> str:
    h = re.search(r"(\d+)H", value)
    m = re.search(r"(\d+)M", value)
    parts = []
    if h:
        parts.append(f"{h.group(1)}h")
    if m:
        parts.append(f"{m.group(1)}m")
    return " ".join(parts)


def person_names(field) -> list[str]:
    if isinstance(field, dict):
        field = [field]
    names = []
    for item in field or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.append(name)
    return names


def base_record(tt_id: str) -> dict:
    return {
        "id": tt_id,
        "kind": "movie",
        "typeLabel": "Title",
        "title": None,
        "year": None,
        "poster": None,
        "plot": None,
        "rating": None,
        "votes": None,
        "contentRating": None,
        "genres": [],
        "runtime": "",
        "directors": [],
        "stars": [],
        "imdbUrl": f"https://www.imdb.com/title/{tt_id}/",
        "source": "basic",
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def normalize_ldjson(tt_id: str, ld: dict) -> dict:
    schema_type = ld.get("@type", "")
    schema_type = schema_type[0] if isinstance(schema_type, list) and schema_type else schema_type

    image = ld.get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    year = None
    date_published = ld.get("datePublished")
    if isinstance(date_published, str) and len(date_published) >= 4 and date_published[:4].isdigit():
        year = int(date_published[:4])

    rating_obj = ld.get("aggregateRating") or {}
    genres = ld.get("genre", [])
    if isinstance(genres, str):
        genres = [genres]

    schema_str = str(schema_type)
    record = base_record(tt_id)
    record.update(
        kind="series" if ("TVSeries" in schema_str or "MiniSeries" in schema_str) else "movie",
        typeLabel=LD_TYPE_LABELS.get(schema_str, "Title"),
        title=ld.get("name"),
        year=year,
        poster=image,
        plot=ld.get("description"),
        rating=rating_obj.get("ratingValue"),
        votes=rating_obj.get("ratingCount"),
        contentRating=ld.get("contentRating"),
        genres=[g for g in genres if isinstance(g, str)],
        runtime=parse_iso_duration(ld.get("duration") or ""),
        directors=(person_names(ld.get("director")) or person_names(ld.get("creator")))[:2],
        stars=person_names(ld.get("actor"))[:3],
        source="full",
    )
    return record


def scrape_full(tt_id: str) -> tuple[dict | None, str]:
    last_error = "unknown error"
    for attempt in range(2):
        try:
            html = http_get(f"https://www.imdb.com/title/{tt_id}/").decode("utf-8", errors="replace")
            ld = extract_ldjson(html)
            if ld is None:
                return None, "page reachable but no structured data found"
            record = normalize_ldjson(tt_id, ld)
            if not record["title"]:
                return None, "record has no title"
            return record, ""
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
            if e.code == 404:
                break
        except Exception as e:
            last_error = type(e).__name__ + ": " + str(e)[:120]
        if attempt == 0:
            time.sleep(RETRY_DELAY)
    return None, last_error


def scrape_basic(tt_id: str) -> tuple[dict | None, str]:
    url = SUGGESTION_URL.format(letter=tt_id[3].lower() if len(tt_id) > 3 else "t", tt_id=tt_id)
    try:
        data = json.loads(http_get(url).decode("utf-8"))
    except Exception as e:
        return None, type(e).__name__ + ": " + str(e)[:120]
    hit = next((x for x in data.get("d", []) if x.get("id") == tt_id), None)
    if hit is None:
        return None, "not found in suggestion index"

    image = (hit.get("i") or {}).get("imageUrl")
    qid = hit.get("qid") or ""
    stars = []
    summary = hit.get("s") or ""
    if summary and not summary.startswith(("Director", "Star")):
        stars = [s.strip() for s in summary.split(",")[:3] if s.strip()]

    record = base_record(tt_id)
    record.update(
        kind="series" if qid in SERIES_TYPES else "movie",
        typeLabel=TYPE_LABELS.get(qid, "Title"),
        title=hit.get("l"),
        year=int(hit["y"]) if isinstance(hit.get("y"), int) else None,
        poster=image,
        stars=stars,
    )
    return record, ""


def parse_minutes(value) -> str:
    m = re.search(r"(\d+)\s*min", str(value or ""))
    if not m:
        return ""
    total = int(m.group(1))
    h, minutes = divmod(total, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def titles_match(a, b) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", str(s or "").lower())
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def enrich_from_cinemeta(record: dict) -> dict:
    hint = "series" if record["kind"] == "series" else "movie"
    other = "movie" if hint == "series" else "series"
    for kind in (hint, other):
        url = CINEMETA_URL.format(kind=kind, tt_id=record["id"])
        try:
            data = json.loads(http_get(url).decode("utf-8"))
        except Exception:
            continue
        meta = data.get("meta") or {}
        if not meta.get("name") or not titles_match(meta.get("name"), record["title"]):
            continue

        enriched = dict(record)
        try:
            enriched["rating"] = float(meta.get("imdbRating"))
        except (TypeError, ValueError):
            pass
        enriched["plot"] = enriched["plot"] or meta.get("description")
        enriched["genres"] = [g for g in (meta.get("genres") or []) if isinstance(g, str)] or enriched["genres"]
        enriched["runtime"] = enriched["runtime"] or parse_minutes(meta.get("runtime"))
        directors = meta.get("director")
        if isinstance(directors, str):
            directors = [directors]
        enriched["directors"] = enriched["directors"] or [d for d in (directors or []) if isinstance(d, str)][:2]
        enriched["stars"] = enriched["stars"] or [s for s in (meta.get("cast") or []) if isinstance(s, str)][:3]
        enriched["source"] = "cinemeta"
        return enriched
    return record


def scrape(tt_id: str) -> tuple[dict | None, str]:
    record, error = scrape_full(tt_id)
    if record is not None:
        return record, ""
    record, basic_error = scrape_basic(tt_id)
    if record is None:
        return None, f"{error}; {basic_error}"
    return enrich_from_cinemeta(record), ""


def needs_upgrade(record: dict) -> bool:
    return record.get("source") == "basic"


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: {CACHE_FILE} unreadable, starting a fresh cache", file=sys.stderr)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


PLACEHOLDER_POSTER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450">'
    '<rect width="100%" height="100%" fill="#1c2333"/>'
    '<text x="50%" y="50%" fill="#4a5573" font-family="sans-serif" '
    'font-size="20" text-anchor="middle">No poster</text></svg>'
)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#dbe1ec;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.5}
header{padding:32px 24px 16px;max-width:1280px;margin:0 auto}
h1{font-size:28px;font-weight:700;letter-spacing:.5px}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 24px 20px;max-width:1280px;margin:0 auto;position:sticky;top:0;background:rgba(13,17,23,.92);backdrop-filter:blur(6px);z-index:5}
select{background:#161b22;border:1px solid #2b3446;border-radius:8px;color:#dbe1ec;padding:9px 10px;font-size:14px;cursor:pointer}
main{max-width:1280px;margin:0 auto;padding:0 24px 64px}
.section{margin-bottom:44px}
.section-head{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid #232b3d;padding-bottom:10px;margin-bottom:16px}
.section-head h2{font-size:21px;font-weight:700;color:#fff;letter-spacing:.3px}
.section-count{color:#6b7689;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}
.card{background:#161b22;border:1px solid #232b3d;border-radius:12px;overflow:hidden;display:flex;flex-direction:column;transition:transform .15s ease,border-color .15s ease}
.card:hover{transform:translateY(-4px);border-color:#3a4763}
.poster-wrap{position:relative;aspect-ratio:2/3;background:#1c2333}
.poster-wrap img{width:100%;height:100%;object-fit:cover;display:block}
.rating-badge{position:absolute;top:10px;left:10px;background:rgba(10,13,18,.85);color:#f5c518;font-weight:700;font-size:14px;padding:3px 9px;border-radius:7px}
.type-badge{position:absolute;top:10px;right:10px;background:rgba(74,108,247,.9);color:#fff;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;padding:3px 8px;border-radius:7px}
.body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.title{font-size:17px;font-weight:700;color:#fff}
.meta{font-size:13px;color:#8a94a8;display:flex;flex-wrap:wrap;gap:6px}
.meta span+span::before{content:"·";margin-right:6px;color:#4a5573}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#212a3d;color:#aeb9cf;font-size:11.5px;padding:2px 9px;border-radius:999px}
.plot{font-size:13.5px;color:#aab3c5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.people{font-size:12.5px;color:#77819a}
.note{border-left:3px solid #f5a524;background:rgba(245,165,36,.08);color:#e8c887;font-size:13px;font-style:italic;padding:7px 10px;border-radius:0 7px 7px 0}
.card-foot{margin-top:auto;display:flex;justify-content:flex-end;padding-top:4px}
.imdb-link{background:#2b3446;color:#e6ebf5;font-size:13px;font-weight:600;text-decoration:none;padding:6px 13px;border-radius:7px}
.imdb-link:hover{background:#4a6cf7}
.empty{text-align:center;color:#6b7689;padding:60px 0;font-size:16px}
footer{max-width:1280px;margin:0 auto;padding:0 24px 40px;color:#4a5573;font-size:12.5px}
@media(max-width:520px){h1{font-size:22px}.grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}.plot,.people,.note{display:none}}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
</header>
<div class="controls">
  <select id="sort">
    <option value="order">List order</option>
    <option value="rating">Rating first</option>
    <option value="year">Newest first</option>
    <option value="title">Title A&ndash;Z</option>
  </select>
</div>
<main><div id="groups"></div><div class="empty" id="empty" hidden>Nothing matches.</div></main>
<footer>Built from <a style="color:#6b7689" href="https://www.imdb.com">IMDb</a> metadata &middot; edit movies.md to change this list</footer>
<script>window.CATALOG=__CATALOG_JSON__;</script>
<script>
(function(){
"use strict";
var PLACEHOLDER="data:image/svg+xml,"+encodeURIComponent(__PLACEHOLDER_JSON__);
var state={sort:"order"};
var groupsRoot=document.getElementById("groups"),empty=document.getElementById("empty");
var GROUPS=[{key:"movie",label:"Movies"},{key:"series",label:"TV Shows"}];

function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}

function buildSections(){
  GROUPS.forEach(function(g){
    var sec=el("section","section");
    var head=el("div","section-head");
    head.appendChild(el("h2",null,g.label));
    var cnt=el("span","section-count","");
    head.appendChild(cnt);
    g._sec=sec;g._count=cnt;
    g._grid=el("div","grid");
    sec.appendChild(head);sec.appendChild(g._grid);
    groupsRoot.appendChild(sec);
  });
}

function fmtVotes(n){if(!n)return"";if(n>=1e6)return(n/1e6).toFixed(1).replace(/\.0$/,"")+"M";if(n>=1e3)return Math.round(n/1e3)+"K";return String(n);}

function metaBits(m){
  var bits=[];
  if(m.year)bits.push(String(m.year));
  if(m.runtime)bits.push(m.runtime);
  if(m.contentRating)bits.push(m.contentRating);
  if(m.rating!=null&&m.votes)bits.push(fmtVotes(m.votes)+" votes");
  return bits;
}

function card(m){
  var c=el("article","card");
  var pw=el("div","poster-wrap");
  var img=document.createElement("img");
  img.loading="lazy";img.alt=m.title||m.id;
  img.src=m.poster||PLACEHOLDER;
  img.onerror=function(){img.onerror=null;img.src=PLACEHOLDER;};
  pw.appendChild(img);
  if(m.rating!=null&&m.rating!=="")pw.appendChild(el("span","rating-badge","\u2605 "+m.rating));
  pw.appendChild(el("span","type-badge",(m.typeLabel||"").toLowerCase()));
  c.appendChild(pw);
  var body=el("div","body");
  body.appendChild(el("div","title",m.title||m.id));
  var meta=el("div","meta");
  metaBits(m).forEach(function(b){meta.appendChild(el("span",null,b));});
  body.appendChild(meta);
  if((m.genres||[]).length){
    var chips=el("div","chips");
    m.genres.slice(0,4).forEach(function(g){chips.appendChild(el("span","chip",g));});
    body.appendChild(chips);
  }
  if(m.plot)body.appendChild(el("p","plot",String(m.plot).replace(/\s+/g," ").trim()));
  var people=[];
  if((m.directors||[]).length)people.push("Dir: "+m.directors.join(", "));
  if((m.stars||[]).length)people.push("With: "+m.stars.join(", "));
  if(people.length)body.appendChild(el("p","people",people.join(" \u00b7 ")));
  if(m.note)body.appendChild(el("div","note",m.note));
  var foot=el("div","card-foot");
  var link=el("a","imdb-link","IMDb \u2197");
  link.href=m.imdbUrl;link.target="_blank";link.rel="noopener";
  foot.appendChild(link);body.appendChild(foot);
  c.appendChild(body);
  return c;
}

function apply(){
  var sorters={
    order:function(a,b){return a._order-b._order;},
    rating:function(a,b){return(b.rating||-1)-(a.rating||-1);},
    year:function(a,b){return(b.year||0)-(a.year||0);},
    title:function(a,b){return String(a.title||"").localeCompare(String(b.title||""));}
  };
  var items=window.CATALOG.slice().sort(sorters[state.sort]);
  var shown=0;
  GROUPS.forEach(function(g){
    var groupItems=items.filter(function(m){return m.kind===g.key;});
    g._grid.replaceChildren.apply(g._grid,groupItems.map(card));
    g._sec.hidden=groupItems.length===0;
    g._count.textContent=groupItems.length+(groupItems.length===1?" title":" titles");
    shown+=groupItems.length;
  });
  empty.hidden=shown>0;
}

document.getElementById("sort").addEventListener("change",function(e){state.sort=e.target.value;apply();});

buildSections();
apply();
})();
</script>
</body>
</html>
"""


def build(list_file: Path, out_file: Path) -> int:
    entries = parse_markdown(list_file)
    cache = load_cache()

    new_ids = [e["id"] for e in entries if e["id"] not in cache]
    upgrade_ids = [
        e["id"] for e in entries
        if e["id"] in cache and needs_upgrade(cache[e["id"]])
    ]
    print(
        f"{len(entries)} unique titles in {list_file.name}, "
        f"{len(new_ids)} new, {len(upgrade_ids)} eligible for enrichment"
    )

    failed = []
    jobs = [("new", tt_id) for tt_id in new_ids] + [("upgrade", tt_id) for tt_id in upgrade_ids]
    for i, (job_kind, tt_id) in enumerate(jobs):
        label = "fetching" if job_kind == "new" else "enriching"
        print(f"{label} {tt_id} ({i + 1}/{len(jobs)})...", end=" ", flush=True)
        record, error = scrape(tt_id)
        if record is None:
            print(f"FAILED ({error})")
            if job_kind == "new":
                failed.append((tt_id, error))
        else:
            cache[tt_id] = record
            src = record.get("source")
            if job_kind == "upgrade" and src == "basic":
                print("no rich source reachable yet, kept basic record")
            else:
                verb = "enriched" if job_kind == "upgrade" else "fetched"
                stars = f" \u2605{record['rating']}" if record.get("rating") is not None else ""
                print(f"{verb} ({src}): {record['title']}{stars}")
        time.sleep(FETCH_DELAY)

    save_cache(cache)

    if annotate_markdown(list_file, entries, cache):
        print(f"updated {list_file.name} with titles")

    catalog = []
    for order, entry in enumerate(entries):
        record = cache.get(entry["id"]) or record_from_label(entry)
        if record is None:
            continue
        item = dict(record)
        item["note"] = entry["note"]
        item["_order"] = order
        catalog.append(item)

    payload = json.dumps(catalog, ensure_ascii=False).replace("<", "\\u003c")
    placeholder_json = json.dumps(PLACEHOLDER_POSTER_SVG)
    html = (
        HTML_TEMPLATE.replace("__TITLE__", PAGE_TITLE)
        .replace("__CATALOG_JSON__", payload)
        .replace("__PLACEHOLDER_JSON__", placeholder_json)
    )
    out_file.write_text(html, encoding="utf-8")

    missing = len(entries) - len(catalog)
    print(f"wrote {out_file} with {len(catalog)} titles ({missing} missing)")
    for tt_id, error in failed:
        print(f"warning: {tt_id} could not be scraped ({error}); will retry next run", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", type=Path, default=DEFAULT_LIST, help=f"path to markdown list (default: {DEFAULT_LIST.name})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output HTML file (default: {DEFAULT_OUT.name})")
    args = ap.parse_args()
    if not args.list.exists():
        print(f"error: {args.list} not found", file=sys.stderr)
        return 1
    return build(args.list, args.out)


if __name__ == "__main__":
    sys.exit(main())
