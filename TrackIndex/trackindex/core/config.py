from __future__ import annotations

from pathlib import Path

APP_NAME = "TrackIndex"
APP_SHORT_NAME = "TrackIndex"
APP_VERSION = "1.0.0"
CONFIG_SCHEMA_VERSION = 5
CONFIG_FILENAME = "trackindex_config.json"
INNO_SETUP_APP_ID = "TrackIndexJustagwas"
MUTEX_NAME = "TrackIndexMutex"

OFFICIAL_PAGE_URL = "https://www.justagwas.com/projects/trackindex"
DOWNLOAD_PAGE_URL = "https://www.justagwas.com/projects/trackindex/download"
UPDATE_MANIFEST_URL = "https://www.justagwas.com/projects/trackindex/latest.json"
UPDATE_SETUP_FALLBACK_URL = "https://downloads.justagwas.com/trackindex/TrackIndexSetup.exe"
UPDATE_CHECK_TIMEOUT_SECONDS = 12.0

UI_SCALE_PERCENT_MIN = 75
UI_SCALE_PERCENT_MAX = 200

SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".aiff", ".aif"}
)
SUPPORTED_PLAYLIST_EXTENSIONS = frozenset({".m3u", ".m3u8"})
SESSION_RECOVERY_FOLDER = ".trackindex-recovery"


def default_playlist_name(folder: Path) -> str:
    name = folder.name.strip() or "playlist"
    return f"{name}.m3u8"
