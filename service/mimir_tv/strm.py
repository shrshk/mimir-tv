"""A provider's shows as Jellyfin TV shows: one .strm file per episode, pointing at the resolver (which signs the
link when Jellyfin plays it). Laid out the way Jellyfin names shows: <Show>/Season NN/<Show> SNNENN - <title>.strm.

A .strm line is <resolver>/play?p=<provider id>&path=<link>; lines written before providers had ids have no p= and
are left as they are (the resolver reads them as the first provider's)."""

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from mimir_tv import badges
from mimir_tv.catalog import Catalog, CatalogError

FOLLOWED = ".followed.json"  # shows added to Jellyfin (title: the show's page), checked for new episodes
MIRRORED = ".mirrored.json"  # what the whole catalogue wrote (mirror.py): {link: {"name": its folder, "at"}}
ADDED = ".added.json"  # movies added to Jellyfin by hand (folder names): the mirror's limit never removes them
LIST_FOR = 6 * 3600  # how old a show's listing may be when it is written
POSTER = "poster.jpg"  # a title's picture, next to its files: Jellyfin reads it on its next scan
POSTER_MAX = 3_000_000


def number(text: str) -> int | None:
    """The season or episode number in a title ("Season 9", "Episode 2. …", "Ep 3")."""
    m = re.search(r"(?:episode|ep|season|s)\s*\.?\s*(\d+)", text, re.I) or re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def play_line(resolver: str, pid: str, url: str, page: str = "") -> str:
    extra = f"&page={quote(page, safe='')}" if page else ""
    return f"{resolver}/play?p={quote(pid, safe='')}&path={quote(url, safe='')}{extra}\n"


def keep(target: Path, line: str, pid: str) -> bool:
    """The file already says this (or said it before providers had ids): left alone, since Jellyfin forgets what
    it knows about a changed file, and a TV play landing then finds nothing to play."""
    if not target.exists():
        return False
    return target.read_text() in (line, line.replace(f"p={quote(pid, safe='')}&", "", 1))


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip().rstrip(".") or "Untitled"


def write_show(
    cat: Catalog, pid: str, show_url: str, title: str, root: Path, resolver: str, ttl: int = LIST_FOR
) -> dict[str, Path]:
    """Writes (or refreshes) a show's .strm files; returns each episode's file by its link. title is the show's
    folder; its episodes are named after the show without the folder's number ("Dark (2)": "Dark S01E01"). An
    episode already written under another name keeps its file: a renamed file is a new item to Jellyfin."""
    show_dir = root / safe(title)
    show_name = re.sub(r" \(\d+\)$", "", safe(title))
    items = cat.page(show_url, ttl=ttl)
    seasons = [i for i in items if i["kind"] == "folder"]
    groups = [(number(s["title"]) or n, cat.page(s["url"], ttl=ttl)) for n, s in enumerate(reversed(seasons), 1)]
    loose = [i for i in items if i["kind"] == "video"]
    if loose:
        groups.append((1 if not seasons else 0, loose))
    files = {}
    for season, episodes in groups:
        folder = show_dir / f"Season {season:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        there = {}  # an episode's link (as its file has it) -> the file
        for f in folder.glob("*.strm"):
            if m := re.search(r"[?&]path=([^&\s]*)", f.read_text()):
                there[m.group(1)] = f
        # Listings are newest first; without a number in the title, the oldest is episode 1.
        for pos, ep in enumerate(reversed([e for e in episodes if e["kind"] == "video"]), 1):
            n = number(ep["title"]) or pos
            label = re.sub(r"^(episode|ep)\s*\.?\s*\d+\s*[.:-]?\s*", "", ep["title"], flags=re.I)
            name = f"{show_name} S{season:02d}E{n:02d}" + (f" - {safe(label)}" if label else "")
            line = play_line(resolver, pid, ep["url"])
            target = there.get(quote(ep["url"], safe=""), folder / f"{name}.strm")
            if not keep(target, line, pid):
                target.write_text(line)
            files[ep["url"]] = target
    return files


def has_poster(folder: Path) -> bool:
    return (folder / POSTER).exists() or (folder / badges.ORIGINAL).exists()


def save_poster(folder: Path, url: str, http: httpx.Client) -> bool:
    """The picture at url as the title's poster (http or https, an image, at most POSTER_MAX bytes); whether it was
    saved. A folder that has one keeps it."""
    if not url.startswith(("http://", "https://")) or not folder.is_dir() or has_poster(folder):
        return False
    try:
        r = http.get(url)
    except httpx.HTTPError:
        return False  # tried again next time
    if (
        r.status_code != 200
        or not r.headers.get("content-type", "").startswith("image/")
        or len(r.content) > POSTER_MAX
    ):
        return False
    (folder / POSTER).write_bytes(r.content)
    return True


def show_poster(cat: Catalog, show_url: str, folder: Path, http: httpx.Client) -> bool:
    """A show added by hand gets its listing's picture when it has none (the whole catalogue's posters reach only
    the shows it lists); whether one was saved."""
    if has_poster(folder):
        return False
    try:
        items = cat.page(show_url, ttl=LIST_FOR)
    except CatalogError:
        return False
    thumb = next((i["thumb"] for i in items if i.get("thumb")), "")
    return save_poster(folder, thumb, http)


def followed(root: Path) -> dict[str, str]:
    f = root / FOLLOWED
    return json.loads(f.read_text()) if f.exists() else {}


def show_folder(root: Path, title: str, show_url: str, cat: Catalog) -> str:
    """The show's folder, which is also its name in Jellyfin: the title, unless another show already uses that
    name, added by hand (the same show from another section) or written by the whole catalogue (the same title in
    another language); then the title with its section. Adding again, or adding a show the whole catalogue wrote,
    reuses its folder."""
    shows = followed(root)
    mirrored = json.loads((root / MIRRORED).read_text()) if (root / MIRRORED).exists() else {}
    key = cat.page_key(show_url)
    for name, url in [*shows.items(), *((r["name"], u) for u, r in mirrored.items())]:
        if cat.page_key(url) == key:
            return name
    name = safe(title)
    taken = {n.lower() for n in shows} | {r["name"].lower() for r in mirrored.values()}  # macOS: case is ignored
    if name.lower() not in taken:
        return name
    label = cat.section_of(show_url)
    other = safe(f"{title} ({label})") if label else ""
    n = 2
    while not other or other.lower() in taken:  # no label, or that one is taken too
        other, n = safe(f"{title} ({n})"), n + 1
    return other


def added(root: Path) -> list[str]:
    f = root / ADDED
    return json.loads(f.read_text()) if f.exists() else []


def add(root: Path, name: str) -> None:
    names = sorted(set(added(root)) | {name})
    root.mkdir(parents=True, exist_ok=True)
    (root / ADDED).write_text(json.dumps(names, indent=1))


def follow(root: Path, title: str, show_url: str) -> None:
    shows = followed(root) | {title: show_url}
    root.mkdir(parents=True, exist_ok=True)
    (root / FOLLOWED).write_text(json.dumps(shows, indent=1, sort_keys=True))


def refresh_shows(cat: Catalog, pid: str, root: Path, resolver: str, ttl: int = 3000) -> int:
    """Writes episodes that appeared since a show was added; returns how many are new. A show the provider fails to
    list is skipped until the next round."""
    new = 0
    for title, url in followed(root).items():
        before = set((root / safe(title)).rglob("*.strm"))
        try:
            files = write_show(cat, pid, url, title, root, resolver, ttl)  # listings at most ttl seconds old
        except CatalogError:
            continue
        new += len(set(files.values()) - before)
    return new


def show_posters(cat: Catalog, root: Path, http: httpx.Client | None = None) -> int:
    """A poster for each show added by hand that has none (show_poster); how many were saved."""
    client = http or httpx.Client(timeout=20, follow_redirects=True)
    try:
        return sum(show_poster(cat, url, root / safe(title), client) for title, url in followed(root).items())
    finally:
        if http is None:
            client.close()


def write_movie(pid: str, url: str, title: str, root: Path, resolver: str, page: str = "") -> Path:
    """<root>/<Title (Year)>/<Title (Year)>.strm, the layout Jellyfin expects for a movie. page: the list it came
    from (a live channel's link expires; the resolver finds the fresh one there)."""
    folder = root / safe(title)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{safe(title)}.strm"
    line = play_line(resolver, pid, url, page)
    if not keep(target, line, pid):
        target.write_text(line)
    return target
