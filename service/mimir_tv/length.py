"""A provider video's real length, read from the file (ffprobe, Jellyfin's own), for its card: TMDB's length is the
film's, and a provider's copy can be another film of the same name or a cut."""

import shutil
import subprocess
from pathlib import Path

PLACES = ("/Applications/Jellyfin.app/Contents/MacOS/ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe")
WAIT = 20  # seconds: a file's header is read, not the file


def ffprobe() -> str:
    return shutil.which("ffprobe") or next((p for p in PLACES if Path(p).exists()), "")


def minutes(link: str) -> int:
    """The file's length in minutes, 0 when it cannot be read (no ffprobe, no answer, no duration)."""
    tool = ffprobe()
    if not tool or not link.startswith("https://"):
        return 0
    try:
        out = subprocess.run(
            [tool, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", link],
            capture_output=True,
            text=True,
            timeout=WAIT,
            check=False,  # a failed read is a 0, from its empty output
        ).stdout
        return round(float(out.strip()) / 60)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0
