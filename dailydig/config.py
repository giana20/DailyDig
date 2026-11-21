"""Settings loaded from DD_* environment variables (see .env.example)."""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field

_INLINE_COMMENT = re.compile(r"\s+#.*$")

def _env(name: str, default: str = "", *, secret: bool = False) -> str:
    v = os.environ.get(f"DD_{name}", default).strip()
    if v.startswith("#"):          # an "inline comment" that the env parser swallowed as the value
        return ""
    if not secret:                 # secrets may legitimately contain " #"
        v = _INLINE_COMMENT.sub("", v).strip()
    return v

def _bool(name: str, default: bool = False) -> bool:
    v = _env(name, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    config_dir: str = field(default_factory=lambda: _env("CONFIG_DIR", "/config"))
    music_dir: str = field(default_factory=lambda: _env("MUSIC_DIR", "/music"))
    # Empty (default) = download straight into the library root. Set e.g. "_discovery" to stage downloads in a
    # subfolder instead (enables auto-expiry of unstarred tracks and promotion of starred ones).
    discovery_subdir: str = field(default_factory=lambda: _env("DISCOVERY_SUBDIR", ""))
    playlists_subdir: str = field(default_factory=lambda: _env("PLAYLISTS_SUBDIR", "_Playlists"))

    lb_user: str = field(default_factory=lambda: _env("LB_USER"))
    lb_token: str = field(default_factory=lambda: _env("LB_TOKEN", secret=True))
    lb_url: str = field(default_factory=lambda: _env("LB_URL", "https://api.listenbrainz.org"))

    navidrome_url: str = field(default_factory=lambda: _env("NAVIDROME_URL"))
    navidrome_user: str = field(default_factory=lambda: _env("NAVIDROME_USER"))
    navidrome_pass: str = field(default_factory=lambda: _env("NAVIDROME_PASS", secret=True))

    yubal_url: str = field(default_factory=lambda: _env("YUBAL_URL"))
    yubal_data_dir: str = field(default_factory=lambda: _env("YUBAL_DATA_DIR", ""))

    # --- downloader -------------------------------------------------------------------------
    # auto | yubal_cli | yubal_api | ytdlp.  auto = yubal_cli if DD_YUBAL_CMD is set, else yubal_api
    # if DD_YUBAL_URL is set, else ytdlp.
    downloader: str = field(default_factory=lambda: _env("DOWNLOADER", "auto").lower())
    # Command template, run without a shell. {url} and {dest} are substituted. Example (Proxmox LXC over ssh):
    #   ssh -o BatchMode=yes root@192.168.68.5 pct exec 100 -- /usr/local/bin/yubal download {url} {dest}
    yubal_cmd: str = field(default_factory=lambda: _env("YUBAL_CMD"))
    # Destination folder AS yubal sees it (inside its container). Default: <yubal data>/_discovery.
    yubal_dest: str = field(default_factory=lambda: _env("YUBAL_DEST", "/app/data"))
    # "yubal_prefix=local_prefix" translating paths printed by yubal into paths in this container.
    yubal_path_map: str = field(default_factory=lambda: _env("YUBAL_PATH_MAP", "/app/data=/music"))
    dl_concurrency: int = field(default_factory=lambda: int(_env("DL_CONCURRENCY", "3")))
    dl_timeout: int = field(default_factory=lambda: int(_env("DL_TIMEOUT", "600")))

    # --- native yt-dlp backend (same options yubal has) ------------------------------------------
    audio_format: str = field(default_factory=lambda: _env("AUDIO_FORMAT", "opus").lower())   # opus|mp3|m4a
    audio_quality: int = field(default_factory=lambda: int(_env("AUDIO_QUALITY", "0")))       # 0 best .. 10
    # Path under the discovery folder. Tokens: {artist} {album_artist} {album} {year} {track} {title} {ext}
    # {track:02d}-style format specs work. Default = yubal's own layout.
    path_template: str = field(default_factory=lambda: _env(
        "PATH_TEMPLATE", "{album_artist}/{year} - {album}/{track:02d} - {title}.{ext}"))
    fetch_lyrics: bool = field(default_factory=lambda: _bool("FETCH_LYRICS", True))
    replaygain: bool = field(default_factory=lambda: _bool("REPLAYGAIN", True))
    ascii_filenames: bool = field(default_factory=lambda: _bool("ASCII_FILENAMES", False))

    region: str = field(default_factory=lambda: _env("REGION", "us").lower())
    tier: str = field(default_factory=lambda: _env("TIER", "core"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8090")))
    contact: str = field(default_factory=lambda: _env("CONTACT", "dailydig@localhost"))

    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", secret=True))
    ingest_token: str = field(default_factory=lambda: _env("INGEST_TOKEN", secret=True))

    workers: int = field(default_factory=lambda: int(_env("WORKERS", "2")))
    expire_days: int = field(default_factory=lambda: int(_env("EXPIRE_DAYS", "0")))   # 0 = keep forever
    auto_download: bool = field(default_factory=lambda: _bool("AUTO_DOWNLOAD", True))
    enable_scheduler: bool = field(default_factory=lambda: _bool("SCHEDULER", True))

    always_review: bool = field(default_factory=lambda: _bool("ALWAYS_REVIEW", False))

    @property
    def db_path(self) -> str:
        return os.path.join(self.config_dir, "dailydig.db")

    @property
    def discovery_dir(self) -> str:
        """Where new downloads go: the library root, or a staging subfolder if DD_DISCOVERY_SUBDIR is set."""
        sub = self.discovery_subdir.strip("/\\")
        return os.path.join(self.music_dir, sub) if sub else self.music_dir

    @property
    def staging(self) -> bool:
        return bool(self.discovery_subdir.strip("/\\"))

    @property
    def playlists_dir(self) -> str:
        return os.path.join(self.music_dir, self.playlists_subdir)

    @property
    def yubal_library(self) -> str:
        """Where yubal writes files, as seen from this container."""
        return self.yubal_data_dir or self.discovery_dir

    @property
    def user_agent(self) -> str:
        return f"dailydig/0.1 ( {self.contact} )"

    @property
    def cookies_path(self) -> str:
        return os.path.join(self.config_dir, "cookies.txt")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
