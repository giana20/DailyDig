# DailyDig

Self-hosted music discovery for a **Navidrome** stack. It builds scheduled playlists
(Daily Dig, Discover Weekly, Release Radar, On Repeat), downloads the tracks with yt-dlp,
verifies every file, delivers the playlists to Navidrome, and learns from what you actually play.

One container, SQLite, ~300 MB RAM. Free/keyless sources (ListenBrainz, Deezer, MusicBrainz, YouTube Music).

## Deploy on your homelab

DailyDig is a normal Docker container. You do **not** rebuild on every start: you build (or pull) the image
once, and only again when the code changes. Your data lives in volumes, so replacing the container never loses
settings, history or the learned model.

### 1. Configure

```bash
git clone https://github.com/YOUR_GITHUB_USER/dailydig.git && cd dailydig
cp .env.example .env        # set DD_LB_USER and the Navidrome URL/user/password
mkdir -p config
```

Edit `docker-compose.yml` and point `/path/to/your/music` at the **same folder Navidrome scans**.
`./config` holds the SQLite database, the learned ranker and your YouTube cookies (back this up).

### 2. Run it: pick one way

**A. Build on the homelab (simplest)**

```bash
docker compose up -d --build
```

To update after you push new code:

```bash
git pull
docker compose up -d --build    # rebuilds only what changed, recreates the container, keeps ./config
```

**B. Use a prebuilt image (no building on the homelab)**

The repo ships `.github/workflows/docker.yml`. Every push to `main` builds the image on GitHub and publishes it to
`ghcr.io/<your-user>/dailydig:latest`. In `docker-compose.yml`, comment out `build: .` and enable the `image:` line, then:

```bash
docker compose pull && docker compose up -d     # update = pull + up
```

If the repo is private, run `docker login ghcr.io` once on the homelab (a GitHub token with `read:packages`), or make
the package public in GitHub → Packages → Package settings. [Watchtower](https://containrrr.dev/watchtower/) can
automate the pull if you want auto-updates.

**C. Portainer / Dockge / Komodo**: create a stack from the Git repository (or paste the compose file) and set the
`.env` values in the UI. Same two options as above.

Then open `http://<homelab-ip>:8090`.

### 3. Optional: feed every scrobble to DailyDig

Set Navidrome's ListenBrainz base URL to `http://dailydig:8090/apis/listenbrainz` (same Docker network, or use the
host IP) and set `DD_INGEST_TOKEN`. DailyDig forwards to real ListenBrainz if `DD_LB_TOKEN` is set.

## Downloader

DailyDig downloads with **yt-dlp** (installed in the image). Settings:

| Variable | Default | Meaning |
|---|---|---|
| `DD_AUDIO_FORMAT` | `opus` | `opus`, `mp3` or `m4a` |
| `DD_AUDIO_QUALITY` | `0` | 0 best … 10 worst (transcodes only) |
| `DD_PATH_TEMPLATE` | `{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}` | folder layout inside the library |
| `DD_FETCH_LYRICS` | `1` | synced `.lrc` from lrclib.net |
| `DD_REPLAYGAIN` | `1` | ReplayGain tags via ffmpeg |
| `DD_DL_CONCURRENCY` | `3` | parallel downloads |

Template tokens: `{artist} {album_artist} {album} {year} {track} {title} {ext}`.
Files are validated (duration, anti-fake bitrate floor) and tagged with MusicBrainz IDs.
If yt-dlp reports "sign in / cookies", upload a Netscape `cookies.txt` in **Downloader → YouTube cookies**.

**Staging folder (optional)**: by default nothing is staged and nothing is ever auto-deleted. If you set
`DD_DISCOVERY_SUBDIR=_discovery`, new tracks go there, unstarred ones expire after `DD_EXPIRE_DAYS`, and starred
ones are moved into the main library.

## How it works

1. **Taste**: incremental ListenBrainz sync + Navidrome stars -> recency-weighted seed artists.
2. **Generators**: LB radio, LB Daily/Weekly Jams, YTM radio, Deezer 2-hop related artists, taste twins, tag radio, fresh releases.
3. **Rank**: Tier A heuristic fusion (CF prior, consensus, genre, novelty, freshness, popularity/skip penalties).
   Tier B personal gradient-boosted model trains automatically after ~200 labelled outcomes.
4. **Assemble**: seen-ledger cooldowns, blocked artists, library dedupe, Thompson-sampling quotas per generator, MMR diversity, one track per artist.
5. **Download**: YTM search -> rule-based match (accept / verify / review / reject) -> yt-dlp -> ffprobe validation -> MusicBrainz ID tags.
   Ambiguous matches land in **Downloader -> Review** for a one-click decision.
6. **Deliver**: M3U8 in `_Playlists/` plus a Subsonic playlist in Navidrome (replaced, never duplicated).
7. **Learn**: played = positive, starred = strong positive, ignored for 10 days = negative.

## Layout

```
dailydig/
  api.py          REST + SSE + LB-compatible ingest      runtime.py   workers, scheduler, janitor
  jobs.py         lease/fence SQLite job queue           ratelimit.py cross-process slot booking
  cache.py        TTL cache, stale-while-revalidate      taste.py     history + profile
  generators.py   candidate sources                      ranker.py    Tier A / Tier B
  playlists.py    build engine + definitions             bandit.py    generator allocation
  download/       matcher, yt-dlp backend, layout, tagging, pipeline
  deliver.py      M3U8 + Navidrome                       cleanup.py   expire / promote
  providers/      ListenBrainz, Deezer, MusicBrainz, YTM
```

## Develop

```bash
pip install -r requirements.txt pytest pytest-asyncio   # tests also need ffmpeg on PATH
python -m pytest
DD_CONFIG_DIR=./config DD_MUSIC_DIR=./music python -m dailydig
```

## Known caveats

- Built and tested offline with mocked services; the first real run against live ListenBrainz/Navidrome
  may need small adjustments (LB response shapes are parsed defensively).
- Deezer/YTM are unofficial or public APIs and can change. Keep yt-dlp current by rebuilding/pulling the image
  regularly: YouTube changes often.
