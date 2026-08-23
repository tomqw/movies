# Movies & TV Shows

A personal catalog page generated from a markdown list of IMDb links.

## Usage

1. Add titles to [`movies.md`](movies.md), one list line each:

   ```markdown
   - [Severance (2022)](https://www.imdb.com/title/tt11280740/) — your note here
   ```

   Just paste the IMDb link and run a build — the `[Title (Year)]` label is
   filled in automatically. Text after the em-dash is your personal note.

2. Push to `main` (or run `python3 build.py` locally). GitHub Actions
   rebuilds `index.html` and publishes it to GitHub Pages.

No installs needed: the build uses only the Python standard library (3.8+).

## How it works

- `build.py` scrapes each title's metadata from IMDb and caches it in
  `data/cache.json`, so every title is fetched once, ever.
- When IMDb's full title pages are unreachable (they sit behind an anti-bot
  wall on some networks), it verifies identity via IMDb's lightweight
  suggestion endpoint and enriches it with rating, plot, genres, and runtime
  from Stremio's public Cinemeta CDN.
- The output is a single self-contained `index.html`: movies and TV shows
  in separate labeled sections, with sorting and a poster grid.
  Open it directly in any browser.
