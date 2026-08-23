# AGENTS.md

Markdown-driven static catalog: `movies.md` (hand-edited list of IMDb links)
→ `build.py` → `index.html` + `data/cache.json` (generated, both committed).
The page is a single self-contained HTML file; posters hotlink from Amazon's CDN.

## Commands

- Full rebuild: `python3 build.py` (options: `--list FILE`, `--out FILE`)
- Requires Python 3.8+, standard library only — never add pip dependencies.
- Node is not part of this project; don't introduce JS build steps.
- When changing the HTML/JS template inside `build.py`, verify by executing the
  generated `index.html` against a DOM (e.g. jsdom): `node --check` alone passes
  scripts that throw at runtime, leaving the card grid silently empty.

## movies.md contract

- Entries are list lines (`- …`) containing an IMDb title URL (`imdb.com/title/tt…`)
  or a bare `tt…` ID. Headers, prose, and other lines are ignored.
- Every build normalizes entry lines to `- [Title (Year)](imdb-url) — personal note`.
  The bracketed label is tool-owned and refreshed from the cache on every run —
  never hand-edit it; a stale/absent cache entry falls back to the existing label.
  Text after the em-dash is the user's permanent personal note.
- Movie vs series comes from fetched data; the file's section headers are cosmetic.
- Duplicates are deduped by tt-ID (first occurrence wins).

## Cache and scraping gotchas

- `data/cache.json` stores every title ever fetched; commit it together with
  `movies.md` changes so CI/deploys never refetch old titles. Delete an entry
  to force a refetch on next build.
- Data resolution per title: IMDb title page JSON-LD → else IMDb suggestion
  endpoint (verified identity: title/year/poster/type/stars) enriched from
  Stremio's public Cinemeta CDN (rating, plot, genres, runtime). Cache records
  carry `"source"`; only `"source": "basic"` records are retried on later builds.
- IMDb full pages sit behind AWS WAF (`x-amzn-waf-action: challenge` from many
  IPs), so most builds run on fallback sources.
- Cinemeta's movie index contains junk rows keyed by unrelated tt-IDs (e.g.
  `/meta/movie/tt0903747.json` returns a film called "Mirror" echoing the
  requested ID back as `imdb_id`) — never trust its identity fields;
  `enrich_from_cinemeta()` only accepts rows whose title matches the
  suggestion-API title (`titles_match`).
- A failed scrape only warns (stderr) and exits 0; the site must never break
  because IMDb was unreachable.

## Deployment

- Push to `main` → `.github/workflows/deploy.yml` rebuilds and publishes to
  GitHub Pages. Repo owner must enable Pages once with Source: "GitHub Actions".
- Never edit `index.html` or cached metadata fields by hand; they are overwritten
  by every build. The page heading text is the `PAGE_TITLE` constant at the top
  of `build.py`.
