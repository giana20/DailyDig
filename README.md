# DailyDig

Self-hosted music discovery for a **Navidrome + yubal** stack. It builds scheduled playlists
(Daily Dig, Discover Weekly, Release Radar, On Repeat), downloads the tracks through yubal,
verifies every file, delivers the playlists to Navidrome, and learns from what you actually play.

One container, SQLite, ~300 MB RAM. Free/keyless sources (ListenBrainz, Deezer, MusicBrainz, YouTube Music).

## Quick start

```bash
cp .env.example .env        # set DD_LB_USER, Navidrome and yubal URLs
# edit docker-compose.yml: point /path/to/your/music at your library
docker compose up -d --build
open http://localhost:8090
```

Downloads go straight into your library (`/music`), laid out like the rest of it. Navidrome must scan that same folder.
Optional: set Navidrome's ListenBrainz base URL to `http://dailydig:8090/apis/listenbrainz` so every
scrobble feeds DailyDig directly (set `DD_INGEST_TOKEN`; DailyDig forwards to real ListenBrainz if `DD_LB_TOKEN` is set).

## Downloaders (`DD_DOWNLOADER`)

| Backend | When to use | Keeps yubal features |
|---|---|---|
| `yubal_cli` | yubal runs elsewhere (e.g. a Proxmox LXC). You give a command template, DailyDig runs it and parses the `OK →` output. | all of them (it *is* yubal) |
| `yubal_api` | yubal's web API is reachable. The job endpoint is discovered from yubal's `/openapi.json`. | all of them |
| `ytdlp` | no yubal needed; runs inside the container. | opus/mp3/m4a, `.lrc` lyrics (lrclib), ReplayGain, dedup, yubal's folder layout |

`auto` picks `yubal_cli` if `DD_YUBAL_CMD` is set, else `yubal_api` if `DD_YUBAL_URL` is set, else `ytdlp`.

**yubal inside a Proxmox LXC over ssh** (what you run in n8n today):

```
DD_YUBAL_CMD=ssh -o BatchMode=yes -i /config/ssh/id_ed25519 root@PROXMOX_IP pct exec 100 -- /usr/local/bin/yubal download {url} {dest}
DD_YUBAL_DEST=/app/data/_discovery      # folder as yubal sees it
DD_YUBAL_PATH_MAP=/app/data=/music      # same folder as this container sees it
DD_DL_CONCURRENCY=3                     # run several at once to hide the slow ssh + pct startup
```

Put a key in `./config/ssh/` and authorize it on the Proxmox host.

**Staging folder (optional)**: by default nothing is staged and nothing is ever auto-deleted. If you set
`DD_DISCOVERY_SUBDIR=_discovery` (and `DD_YUBAL_DEST=/app/data/_discovery` for yubal), new tracks go there,
unstarred ones expire after `DD_EXPIRE_DAYS`, and starred ones are moved into the main library.

**Native layout**: `DD_PATH_TEMPLATE` controls where `ytdlp` puts files, using `{artist} {album_artist} {album} {year} {track} {title} {ext}`.
The default (`{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}`) is yubal's layout.

## How it works

1. **Taste**: incremental ListenBrainz sync + Navidrome stars -> recency-weighted seed artists.
2. **Generators**: LB radio, LB Daily/Weekly Jams, YTM radio, Deezer 2-hop related artists, taste twins, tag radio, fresh releases.
3. **Rank**: Tier A heuristic fusion (CF prior, consensus, genre, novelty, freshness, popularity/skip penalties).
   Tier B personal gradient-boosted model trains automatically after ~200 labelled outcomes.
4. **Assemble**: seen-ledger cooldowns, blocked artists, library dedupe, Thompson-sampling quotas per generator, MMR diversity, one track per artist.
5. **Download**: YTM search -> rule-based match (accept / verify / review / reject) -> backend (above) -> ffprobe validation (duration, anti-fake bitrate floor) -> MusicBrainz ID tags.
   Ambiguous matches land in **Downloader -> Review** for a one-click decision.
6. **Deliver**: M3U8 in `_Playlists/` (same folder yubal uses) plus a Subsonic playlist in Navidrome (replaced, never duplicated).
7. **Learn**: played = positive, starred = strong positive, ignored for 10 days = negative. (With a staging folder, unstarred files expire and starred ones are promoted.)

## Layout

```
dailydig/
  api.py          REST + SSE + LB-compatible ingest      runtime.py   workers, scheduler, janitor
  jobs.py         lease/fence SQLite job queue           ratelimit.py cross-process slot booking
  cache.py        TTL cache, stale-while-revalidate      taste.py     history + profile
  generators.py   candidate sources                      ranker.py    Tier A / Tier B
  playlists.py    build engine + definitions             bandit.py    generator allocation
  download/       matcher, backends (yubal CLI/API, yt-dlp), layout, tagging, pipeline
  deliver.py      M3U8 + Navidrome                       cleanup.py   expire / promote
  providers/      ListenBrainz, Deezer, MusicBrainz, YTM
```

## Develop

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest            # 30 tests
DD_CONFIG_DIR=./config DD_MUSIC_DIR=./music python -m dailydig
```

## Known caveats

- Built and tested offline with mocked services; the first real run against live ListenBrainz/yubal/Navidrome
  may need small adjustments (yubal's API schema and LB response shapes are parsed defensively).
- Deezer/YTM are unofficial or public APIs and can change.
