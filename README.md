# MediaFinder

A small self-hosted dashboard that indexes your media library and lets you search it and export the title list. It runs in a Linux container and can read from:

- **Jellyfin** – via its REST API (auto-discovered on your network; sign in with your account or an API key), and/or
- **A NAS or folder** – connects over SMB (or reads a mounted folder) and scans files and identifies titles from naming conventions, the same way Jellyfin does (`Movie (2010)/Movie (2010).mkv`, `Show/Season 01/S01E01.mkv`). Parsing is done with [guessit](https://github.com/guessit-io/guessit), so messy release-style names work too.

Both sources can be enabled together. Titles are stored in a local SQLite database with full-text (prefix) search.

## Features

- Instant search across titles, descriptions and genres
- Filter by type (movie / series / album), library, source
- Export the title list as **CSV, plain text or JSON** (honours your current search/filters, de-duplicates titles found in both sources)
- Manual "Sync now" button plus optional periodic re-sync
- Single container, no external services

## Quick start (Docker)

```sh
docker compose up -d --build
```

Open <http://localhost:8080>, go to **Settings**, and set things up from the browser - no config files needed.

### Connect Jellyfin
1. Click **Find servers on my network**. MediaFinder sends the same UDP broadcast Jellyfin's own apps use and lists every server that answers. (You can also enter an IP range to scan, or just type the address.)
2. Sign in with your Jellyfin username and password, or paste an API key (Dashboard → Advanced → API Keys). Only an access token is stored, never your password.

### Connect your NAS
- **Network share (SMB)** - click **Find NAS on my network** (scans a range for SMB hosts), pick a host or type its address, enter credentials, **List shares**, then browse into the folder you want and **Add**. Works with Synology, QNAP, TrueNAS, Unraid, Windows and Samba. Nothing is mounted, so the container needs no special privileges.
- **Folder inside the container** - if you'd rather mount the NAS yourself (NFS, CIFS, bind mount), pick the mounted folder with the built-in folder browser.

For each folder you choose whether it *contains* library folders (`Movies/`, `TV/`...) or *is* a single library, and optionally force "movies" or "TV shows" if auto-detection guesses wrong.

Then press **Sync now** (or set an automatic interval).

### Networking note
Broadcast discovery only reaches your LAN when the container uses **host networking** (the default in `docker-compose.yml`). If you switch to a bridged network, discovery still works by scanning an IP range you type into the UI.

Settings (including NAS credentials and the Jellyfin token) are saved in `/data/config.json` with owner-only permissions. Keep `/data` private.

## Configuration (optional)

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/mediafinder.db` | SQLite location; `config.json` is stored beside it |
| `SYNC_ON_START` | `true` | Sync when the container starts |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `MEDIA_PATHS`, `SYNC_INTERVAL_MINUTES` | - | Pre-seed a fresh install (otherwise use the UI) |

## API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=&type=&library=&source=&year=&limit=&offset=` | Search |
| `GET /api/export?format=csv\|txt\|json&...filters` | Download title list |
| `GET /api/libraries`, `GET /api/stats` | Overview |
| `POST /api/sync`, `GET /api/sync/status` | Trigger / check a sync |
| `GET /api/discover/jellyfin`, `GET /api/discover/smb?cidr=` | Find servers / NAS hosts |
| `POST /api/smb/shares`, `POST /api/smb/browse`, `GET /api/fs/browse` | Browse shares and folders |
| `GET /api/config`, `POST /api/jellyfin/connect`, `POST/DELETE /api/sources` | Manage connections |

There is no authentication built in – run it on your LAN or behind a reverse proxy that handles auth.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
DB_PATH=./data/dev.db uvicorn mediafinder.app:app_factory --factory --reload
```

## Similar projects

Jellyfin has its own search but no title-list export; [Jellystat](https://github.com/CyferShepard/Jellystat) focuses on playback statistics; Radarr/Sonarr track what you *monitor* rather than what you have on disk. MediaFinder is a lightweight "what do I own, give me the list" tool.

## License

MIT
