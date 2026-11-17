# MediaFinder

A small self-hosted dashboard that indexes your media library and lets you search it and export the title list. It runs in a Linux container and can read from:

- **Jellyfin** – via its REST API (just needs a URL and an API key), and/or
- **A folder / NAS mount** – scans files and identifies titles from naming conventions, the same way Jellyfin does (`Movie (2010)/Movie (2010).mkv`, `Show/Season 01/S01E01.mkv`). Parsing is done with [guessit](https://github.com/guessit-io/guessit), so messy release-style names work too.

Both sources can be enabled together. Titles are stored in a local SQLite database with full-text (prefix) search.

## Features

- Instant search across titles, descriptions and genres
- Filter by type (movie / series / album), library, source
- Export the title list as **CSV, plain text or JSON** (honours your current search/filters, de-duplicates titles found in both sources)
- Manual "Sync now" button plus optional periodic re-sync
- Single container, no external services

## Quick start (Docker)

```sh
cp .env.example .env      # fill in JELLYFIN_URL / JELLYFIN_API_KEY and/or MEDIA_PATHS
docker compose up -d --build
```

Open <http://localhost:8080> and press **Sync now**.

To scan a NAS folder, mount it read-only in `docker-compose.yml` (see the commented volume) and set `MEDIA_PATHS=/media`. Point `MEDIA_PATHS` at the **parent** directory that contains your library folders (e.g. `/media` containing `Movies/` and `TV/`) – each subfolder becomes a library.

Create a Jellyfin API key under **Dashboard → Advanced → API Keys**.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `JELLYFIN_URL` | – | e.g. `http://jellyfin:8096` |
| `JELLYFIN_API_KEY` | – | Jellyfin API key |
| `MEDIA_PATHS` | – | Comma-separated folders (inside the container) to scan |
| `SYNC_INTERVAL_MINUTES` | `0` | Auto re-sync interval; `0` = manual only |
| `SYNC_ON_START` | `true` | Sync when the container starts |
| `DB_PATH` | `/data/mediafinder.db` | SQLite location (mount `/data` to persist) |

## API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=&type=&library=&source=&year=&limit=&offset=` | Search |
| `GET /api/export?format=csv\|txt\|json&...filters` | Download title list |
| `GET /api/libraries`, `GET /api/stats` | Overview |
| `POST /api/sync`, `GET /api/sync/status` | Trigger / check a sync |

There is no authentication built in – run it on your LAN or behind a reverse proxy that handles auth.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
DB_PATH=./data/dev.db MEDIA_PATHS=/path/to/media uvicorn mediafinder.app:app_factory --factory --reload
```

## Similar projects

Jellyfin has its own search but no title-list export; [Jellystat](https://github.com/CyferShepard/Jellystat) focuses on playback statistics; Radarr/Sonarr track what you *monitor* rather than what you have on disk. MediaFinder is a lightweight "what do I own, give me the list" tool.

## License

MIT
