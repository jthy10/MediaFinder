# MediaFinder

A small self-hosted dashboard that indexes your media library and lets you search it and export the title list. It runs in a Linux container and can read from:

- **Jellyfin** – via its REST API (auto-discovered on your network; sign in with your account or an API key), and/or
- **A NAS or folder** – connects over SMB (or reads a mounted folder) and scans files and identifies titles from naming conventions, the same way Jellyfin does (`Movie (2010)/Movie (2010).mkv`, `Show/Season 01/S01E01.mkv`). Parsing is done with [guessit](https://github.com/guessit-io/guessit), so messy release-style names work too.

Both sources can be enabled together. Titles are stored in a local SQLite database with full-text (prefix) search.

## Features

- Instant search across titles, descriptions and genres
- Filter by type (movie / series / album), library, source
- Export the title list as **CSV, plain text or JSON** (honours your current search/filters, de-duplicates titles found in both sources)
- Manual "Sync now" button plus optional periodic re-sync, with live per-source progress and the result of the last run
- Incremental syncs: only changed titles are written, so large libraries stay fast
- Compare any two sources, identify titles via TMDB, correct or hide wrong matches
- Single container, no external services

## Install

Pick whichever fits your homelab. All of them end at the same web UI on port **8080**.

### Docker / Compose (Portainer, Unraid, TrueNAS, Synology, plain Docker)
A multi-arch image (amd64 + arm64) is published to GitHub Container Registry on every release of `main`:

```sh
curl -O https://raw.githubusercontent.com/jthy10/MediaFinder/main/docker-compose.yml
docker compose up -d
```
Or just `docker run -d --network host -v mediafinder-data:/data ghcr.io/jthy10/mediafinder:latest`.
The image runs as a non-root user, keeps its data in the `/data` volume, and has a healthcheck.

### Proxmox (one command, run on the Proxmox host)
Creates a small unprivileged Debian 12 LXC (1 core, 512 MB, 4 GB) and installs MediaFinder as a systemd service inside it:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/jthy10/MediaFinder/main/deploy/proxmox-lxc.sh)"
```
Tweak with `CTID`, `STORAGE`, `BRIDGE`, `MEMORY`, `DISK_GB`. Give the container a DHCP reservation so the address stays put.

### Any Debian/Ubuntu box, VM or existing LXC (no Docker)
```sh
curl -fsSL https://raw.githubusercontent.com/jthy10/MediaFinder/main/deploy/install.sh | sudo bash
sudo bash /opt/mediafinder/deploy/install.sh update      # later: update in place
```
Config lives in `/etc/mediafinder.env`, data in `/var/lib/mediafinder`.

### Then, in the browser
Open `http://<host>:8080`, go to **Settings**, and set everything up there - no config files needed. Set a password first (Settings → Security).

### Connect Jellyfin
1. Click **Find servers on my network**. MediaFinder sends the same UDP broadcast Jellyfin's own apps use and lists every server that answers. (You can also enter an IP range to scan, or just type the address.)
2. Sign in with your Jellyfin username and password, or paste an API key (Dashboard → Advanced → API Keys). Only an access token is stored, never your password.

### Connect your NAS
- **Network share (SMB)** - click **Find NAS on my network** (scans a range for SMB hosts), pick a host or type its address, enter credentials, **List shares**, then browse into the folder you want and **Add**. Works with Synology, QNAP, TrueNAS, Unraid, Windows and Samba over **SMB 2/3 (signing and encryption supported)**. Nothing is mounted, so the container needs no special privileges. If a server doesn't list its shares automatically, just type the share name.
- **Folder inside the container** - if you'd rather mount the NAS yourself (NFS, CIFS, bind mount), pick the mounted folder with the built-in folder browser.

For each folder you choose whether it *contains* library folders (`Movies/`, `TV/`...) or *is* a single library, and optionally force "movies" or "TV shows" if auto-detection guesses wrong.

Then press **Sync now** (or set an automatic interval).

### Compare sources
The **Compare** tab shows what one source has that another doesn't, for example files on your NAS that Jellyfin hasn't picked up, or the reverse, and exports the differences as CSV. Titles are matched by TMDB / IMDb / TVDB id when known, otherwise by title and year (a Dune 1984 file won't match Dune 2021).

### Better identification (optional TMDB key)
File names are messy, so MediaFinder can look titles up on [TMDB](https://www.themoviedb.org/settings/api) (free key, **Settings → Title identification**). It fixes names and years (`scream 2022` becomes *Scream (2022)*), corrects movie-vs-series mix-ups and records TMDB/IMDb/TVDB ids, which makes comparing with Jellyfin exact. Lookups are cached, so repeat syncs are free.

Ids you already put in names are used automatically and are the most reliable way to identify something. All of these work in file or folder names (Jellyfin/Sonarr/Radarr style):

```
The Matrix (1999) [tmdbid-603]/
Breaking Bad {tvdb-81189}/Season 01/...
Heat (1995) (imdbid=tt0113277).mkv        Heat.1995.tt0113277.mkv
```

Still wrong? Click ✎ next to any NAS/folder title to fix the title, year, type or ids, or to hide junk like samples and extras. Edits survive re-syncs and can be undone under **Settings → Corrections**.

### Networking note
Broadcast discovery only reaches your LAN when the container uses **host networking** (the default in `docker-compose.yml`; the Proxmox LXC and systemd installs are on your LAN already). If you switch to a bridged network, discovery still works by scanning an IP range you type into the UI.

Settings (including NAS credentials and the Jellyfin token) are saved in `/data/config.json` with owner-only permissions. Keep `/data` private.

## Configuration (optional)

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/mediafinder.db` | SQLite location; `config.json` is stored beside it |
| `MEDIAFINDER_PASSWORD` | - | Require this password (otherwise set one in the UI) |
| `MEDIAFINDER_SECRET_KEY` | - | Key for encrypting stored credentials (default: a key file beside the config) |
| `BROWSE_ROOTS` | `/media,/mnt,/srv` | Folders the in-app folder browser may access (`*` = anywhere) |
| `PORT` | `8080` | Listen port (Docker image / systemd install) |
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
| `GET /api/config`, `POST /api/jellyfin/connect`, `POST/DELETE /api/sources`, `POST/DELETE /api/tmdb` | Manage connections |
| `GET /api/compare?a=&b=&format=json\|csv` | Titles only in A / only in B (`@folders`, `@jellyfin` or source labels) |
| `PUT /api/items/{id}`, `GET /api/overrides`, `DELETE /api/overrides/{id}` | Correct, hide or restore titles |

## Security

- **Password:** set one in **Settings → Security** (or pass `MEDIAFINDER_PASSWORD`). Until you do, the UI shows a warning and anyone who can reach the page can use it. Sessions use an HttpOnly, SameSite=Strict cookie and failed logins are throttled.
- **Stored credentials are encrypted** (NAS passwords, Jellyfin token, TMDB key) in `config.json`. The key is `MEDIAFINDER_SECRET_KEY` if you set it, otherwise a `secret.key` file next to the config. Setting the env var keeps the key out of your data folder/backups. Lose the key and you just re-enter your connections.
- **Folder access is scoped:** the in-app folder browser and "local folder" sources are limited to `BROWSE_ROOTS` (default `/media,/mnt,/srv`; `*` = anywhere). Symlink and `..` escapes are blocked.
- **Network scans** are limited to private (LAN) ranges.
- Serving it beyond your LAN? Put it behind a TLS reverse proxy (Caddy, Nginx Proxy Manager, Traefik).

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
