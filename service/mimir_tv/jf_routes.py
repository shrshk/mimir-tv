"""Your own videos (Jellyfin) in mimir-tv: browse and search. Jellyfin plays them first (its web app on the
phone, its TV app on the TV); playing here and Kodi on the TV are the other options.

The browser never talks to Jellyfin: videos, HLS playlists and images come through /jf/..., which adds the API
key on the Air. Files Safari plays as they are (MP4/MOV with H.264/HEVC) stream unchanged; anything else goes
through Jellyfin's HLS, which converts on the Air's hardware."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from mimir_tv.jellyfin import Jellyfin, JellyfinError
from mimir_tv.kodi import Kodi, KodiError
from mimir_tv.tv import Tvs, TvError, aimed, tv_address
from mimir_tv.mylist import LIVE_LIST, ListDates

FOLDERS = {"CollectionFolder", "Folder", "Series", "Season", "BoxSet", "UserView", "PhotoAlbum"}
PLAYABLE = {"Movie", "Episode", "Video", "MusicVideo"}
DIRECT_CONTAINERS = {"mp4", "m4v", "mov"}
DIRECT_VIDEO = {"h264", "hevc"}
DIRECT_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "flac", None}
# As Jellyfin's own players: under 5% watched keeps no position, from 90% on it counts as played.
MIN_RESUME, MAX_RESUME = 0.05, 0.9
ID = r"^[0-9a-f]{32}$"
# What /jf/ passes to Jellyfin: one video's stream, playlists and HLS segments, or one item's image. Every part is a
# plain name (never "." or ".."): the path arrives decoded, and the client would resolve dots to any other API.
PART = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
PASSABLE = re.compile(rf"Videos/[0-9a-f]{{32}}(/{PART})+|Items/[0-9a-f]{{32}}/Images/{PART}(/[0-9]+)?")
PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "last-modified")


class Play(BaseModel):
    id: str = Field(pattern=ID)
    tv: str = Field("", max_length=100)  # which TV (its name in Settings); the first one when empty


class Watched(BaseModel):
    id: str = Field(pattern=ID)
    on: bool


def which_tv(tvs: Tvs | None, name: str) -> str:
    """The address of the TV a request names ("" for the default TV); 404 when Settings has no such TV."""
    try:
        return tv_address(tvs, name)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


class Watching(BaseModel):
    """What Play here is playing: sent at the start, about every 10 s and on pause, and when it stops."""

    id: str = Field(pattern=ID)
    session: str = Field(pattern=r"^[0-9a-f]{32}$")
    event: Literal["start", "progress", "stopped"]
    position: float = Field(ge=0)  # seconds
    duration: float = Field(default=0, ge=0)  # seconds, for an item Jellyfin knows no length of
    paused: bool = False


def words(text: str) -> list[str]:
    """Lowercase words without accents or punctuation: "M.S. Dhoni: Café" -> ["m", "s", "dhoni", "cafe"]."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", plain.lower())


SHORT = 3  # a search word this short only matches the start of a word, or initials ("nba" is not in "Nanbagal")
TYPO = 5  # a search word this long also matches a word one letter off ("telgu", "Telugu"), as Jellyfin forgives


def slip(a: str, b: str) -> bool:
    """a and b differ by one letter at most: one left out, added or changed."""
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + 1 :] == b[i + 1 :] or a[i:] == b[i + 1 :] or a[i + 1 :] == b[i:]


def closeness(name: str, q: str) -> int:
    """Wholphin's order within a row (SearchRelevance): the exact name, then a name starting with the search, then
    one with a word starting with it, then one containing it, then the rest; lower is closer."""
    name, q = name.lower(), q.lower().strip()
    if name == q:
        return 0
    if name.startswith(q):
        return 100
    if f" {q}" in name:
        return 200
    return 300 if q in name else 400


KIND_ORDER = {kind: n for n, kind in enumerate(Jellyfin.SEARCHED)}  # rows: movies, shows, episodes, collections …


def named(q: str, i: dict) -> bool:
    """Every word of the search (of two letters or more) is in the item's name, its original name or its show's name,
    in any order; a short word (SHORT letters or fewer) only as the start of a word or as initials. Jellyfin's own
    search is loose: "M.S. Dhoni: The Untold Story" also finds M*A*S*H and McCloud."""
    found = words(" ".join(filter(None, (i.get("Name"), i.get("OriginalTitle"), i.get("SeriesName")))))
    hay = " ".join(found)
    initials, run = set(), ""  # runs of one-letter words: "M.S. Dhoni" has "ms"
    for w in [*found, ""]:
        if len(w) == 1:
            run += w
        elif run:
            initials.add(run)
            run = ""

    def has(w: str) -> bool:
        if len(w) > SHORT:
            return w in hay or w in hay.replace(" ", "") or (len(w) >= TYPO and any(slip(w, x) for x in found))
        return any(x.startswith(w) for x in found) or any(w in run for run in initials)

    return all(has(w) for w in words(q) if len(w) > 1)


def wide(i: dict) -> str:
    """A landscape picture for Wholphin-style wide rows (Continue watching): a movie's or show's backdrop, an
    episode's, a video's or a channel's own picture (a still); "" when there is only a poster."""
    if i.get("BackdropImageTags"):
        return f"/jf/Items/{i['Id']}/Images/Backdrop?maxWidth=780&quality=85"
    if i.get("Type") in ("Episode", "Video", "TvChannel") and (i.get("ImageTags") or {}).get("Primary"):
        return f"/jf/Items/{i['Id']}/Images/Primary?maxWidth=780&quality=85"
    if i.get("ParentBackdropItemId") and i.get("ParentBackdropImageTags"):
        return f"/jf/Items/{i['ParentBackdropItemId']}/Images/Backdrop?maxWidth=780&quality=85"
    return ""


# A guide's filler for a slot it knows nothing about: not a programme to show.
FILLER = re.compile(r"^\s*(schedule not available|no information|to be announced|tba|no programme)\b", re.I)


def programme(p: dict) -> dict:
    """A guide programme as a card that plays its channel: the programme's name, the channel's name and logo, its
    times, and how far in it is when on now."""
    start, end = p.get("StartDate") or "", p.get("EndDate") or ""
    now = datetime.now(UTC)
    on_now = start[:19] <= now.isoformat()[:19] < end[:19]
    progress = 0
    if on_now:
        t0, t1 = (datetime.fromisoformat(x[:19]).replace(tzinfo=UTC) for x in (start, end))
        progress = round(100 * (now - t0) / (t1 - t0)) if t1 > t0 else 0
    return {
        "id": p["ChannelId"],
        "node": f"jf:{p['ChannelId']}",
        "kind": "video",
        "type": "TvChannel",
        "title": p.get("Name", ""),
        "channel": p.get("ChannelName") or "",
        "desc": p.get("Overview") or "",
        "start": start,
        "end": end,
        "on_now": on_now,
        "progress": progress,
        "thumb": f"/jf/Items/{p['ChannelId']}/Images/Primary?maxHeight=480&quality=85",
    }


def card(i: dict) -> dict:
    kind = "folder" if i.get("Type") in FOLDERS or i.get("IsFolder") else "video"
    sub = " · ".join(str(x) for x in (i.get("SeriesName"), i.get("ProductionYear")) if x)
    if i.get("Type") == "Episode" and i.get("IndexNumber"):
        sub = " · ".join(
            filter(None, [i.get("SeriesName"), f"S{i.get('ParentIndexNumber') or 0:02d}E{i['IndexNumber']:02d}"])
        )
    if i.get("RunTimeTicks") and kind == "video":
        sub = " · ".join(filter(None, [sub, f"{round(i['RunTimeTicks'] / 6e8)} min"]))
    if i.get("Type") == "TvChannel":  # a live channel (IPTV): no length, no year
        sub = "Live TV"
    pos = (i.get("UserData") or {}).get("PlaybackPositionTicks") or 0
    return {
        "title": i.get("Name", ""),
        "node": f"jf:{i['Id']}",
        "id": i["Id"],
        "kind": kind,
        "type": i.get("Type"),
        "sub": sub,
        "desc": i.get("Overview") or "",
        "thumb": f"/jf/Items/{i['Id']}/Images/Primary?maxHeight=480&quality=85"
        if i.get("ImageTags", {}).get("Primary")
        else f"/jf/Items/{i['SeriesId']}/Images/Primary?maxHeight=480&quality=85"  # an episode without its own
        if i.get("SeriesId") and i.get("SeriesPrimaryImageTag")
        else "",
        "progress": round(100 * pos / i["RunTimeTicks"]) if pos and i.get("RunTimeTicks") else 0,
        "minutes": round(i["RunTimeTicks"] / 6e8) if i.get("RunTimeTicks") else 0,
        "played": bool((i.get("UserData") or {}).get("Played")),
        "wide": wide(i),
        # An episode's show, for its sheet's "Go to show" (Next up, Continue watching, search)
        "show": f"jf:{i['SeriesId']}" if i.get("SeriesId") else "",
        "show_title": i.get("SeriesName") or "",
    }


def since(date: str | None) -> float:
    """A Jellyfin date (UTC, up to 7 decimals) in seconds; 0 when there is none."""
    try:
        return datetime.fromisoformat(date[:19]).replace(tzinfo=UTC).timestamp() if date else 0.0
    except ValueError:
        return 0.0


def play_source(item: dict) -> str:
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or []
    video = next((s.get("Codec") for s in streams if s.get("Type") == "Video"), None)
    audio = next((s.get("Codec") for s in streams if s.get("Type") == "Audio"), None)
    containers = set((src.get("Container") or "").split(","))
    sid = src.get("Id", item["Id"])
    if containers & DIRECT_CONTAINERS and video in DIRECT_VIDEO and audio in DIRECT_AUDIO:
        return f"/jf/Videos/{item['Id']}/stream?static=true&MediaSourceId={sid}"
    return (
        f"/jf/Videos/{item['Id']}/master.m3u8?MediaSourceId={sid}&VideoCodec=h264&AudioCodec=aac"
        "&TranscodingMaxAudioChannels=2&SegmentContainer=mp4&MinSegments=1&BreakOnNonKeyFrames=True"
        "&VideoBitrate=8000000&AudioBitrate=192000"
    )


def router(
    jf: Jellyfin,
    kodi: Kodi | None,
    tv_base: str,
    open_app=None,
    dates: ListDates | None = None,
    hidden: Callable[[], set[str]] = set,
    play_app=None,
    phone_app: Callable[[], str] = lambda: "swiftfin",
    tvs: Tvs | None = None,
    live_roots: Callable[[], list[str]] = list,
    fetch: Callable[[str], str] | None = None,
) -> APIRouter:
    """hidden: the providers' own libraries, shown in each provider's section instead of Your library. tvs: the TVs
    in Settings, so a play can name one (open_app and play_app then take its address as tv=). live_roots: the
    folders of the providers' live channels (their .strm files), whose channels go on My list's own playlist."""
    api = APIRouter()

    def guard(fn, *args):
        try:
            return fn(*args)
        except (JellyfinError, TvError) as e:
            raise HTTPException(502, str(e)) from e

    @api.get("/api/jf/browse")
    def browse(id: str = ""):
        if not id:
            skip = hidden()
            views = [v for v in guard(jf.views) if v.get("Name") not in skip and v.get("CollectionType") != "playlists"]
            return {"title": "Your library", "items": [card(v) | {"kind": "folder"} for v in views]}
        parent = guard(jf.item, id)
        return {
            "title": parent.get("Name", ""),
            "desc": parent.get("Overview") or "",
            "items": [card(i) for i in guard(jf.children, id)],
        }

    @api.get("/api/jf/search")
    def search(q: str):
        # TvChannel: live channels from an IPTV provider (M2), played by Jellyfin's Live TV
        hits = [i for i in guard(jf.search, q) if i.get("Type") in KIND_ORDER and named(q, i)]
        hits.sort(
            key=lambda i: (KIND_ORDER[i["Type"]], closeness(i.get("Name") or "", q))
        )  # stable: Jellyfin's order next
        return [card(i) for i in hits]

    @api.get("/api/jf/programmes")
    def programmes(q: str = Query(max_length=200)):
        """What is on TV now or later today with every word of the search in its name (the guide: Jellyfin's Live
        TV), on now first, then by start. Each card is its channel's, which plays it."""
        if not any(len(w) > 1 for w in words(q)):
            return []
        out = [programme(p) for p in guard(jf.programmes, q) if p.get("ChannelId") and named(q, p)]
        return sorted(out, key=lambda x: (not x["on_now"], x["start"]))

    fetch = fetch or (lambda url: httpx.get(url, timeout=30, follow_redirects=True).text)
    chans: dict = {"at": 0.0, "list": [], "groups_at": 0.0, "groups": {}}  # Live TV's channels; the tuners' groups

    def channel_list() -> list[dict]:
        """Jellyfin's channels, kept two minutes (5,000 and more: asked for once, not on every page)."""
        if time.time() - chans["at"] > 120:
            chans["list"], chans["at"] = guard(jf.all_channels), time.time()
        return chans["list"]

    def tuner_groups() -> dict[str, str]:
        """A channel's group (group-title) by its name, read from the playlists Live TV's tuners read (Jellyfin keeps
        no groups), in the playlists' order; kept an hour."""
        if time.time() - chans["groups_at"] > 3600:
            groups: dict[str, str] = {}
            try:
                for url in jf.tuner_urls():
                    for m in re.finditer(r'#EXTINF[^\n]*?group-title="([^"]*)"[^\n]*,([^\n]*)', fetch(url)):
                        groups.setdefault(m.group(2).strip(), m.group(1).strip())
            except (JellyfinError, httpx.HTTPError):
                pass  # no groups this time
            chans["groups"], chans["groups_at"] = groups, time.time()
        return chans["groups"]

    @api.get("/api/jf/channels")
    def channels(
        group: str = Query("", max_length=200),
        fav: bool = False,
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=200),
        hours: int = Query(3, ge=1, le=12),
    ):
        """Live TV as Jellyfin's apps list it (favourites first, then by number), in its tuner's groups, a page at a
        time; each channel with its guide for the next hours (the Channels and the Guide views)."""
        groups = tuner_groups()
        listed = channel_list()
        counts: dict[str, int] = {}
        for c in listed:
            g = groups.get(c.get("Name", "").strip())
            if g:
                counts[g] = counts.get(g, 0) + 1
        order = list(dict.fromkeys(groups.values()))
        wanted = [
            c
            for c in listed
            if (not group or groups.get(c.get("Name", "").strip()) == group)
            and (not fav or (c.get("UserData") or {}).get("IsFavorite"))
        ]
        page = wanted[offset : offset + limit]
        now = time.time()
        shows: dict[str, list[dict]] = {}
        if page:
            for p in guard(jf.guide, [c["Id"] for c in page], now, now + hours * 3600):
                if p.get("ChannelId") and not FILLER.match(p.get("Name") or ""):
                    shows.setdefault(p["ChannelId"], []).append(programme(p))
        keep = ("title", "start", "end", "on_now", "progress")
        return {
            "groups": [{"name": g, "count": counts[g]} for g in order if counts.get(g)],
            "total": len(wanted),
            "channels": [
                {
                    "id": c["Id"],
                    "name": c.get("Name", ""),
                    "number": c.get("ChannelNumber") or "",
                    "thumb": card(c)["thumb"],
                    "fav": bool((c.get("UserData") or {}).get("IsFavorite")),
                    "group": groups.get(c.get("Name", "").strip(), ""),
                    "programmes": [{k: p[k] for k in keep} for p in shows.get(c["Id"], [])],
                }
                for c in page
            ],
        }

    @api.get("/api/jf/live")
    def live_tv():
        """The Live TV tab: what the guide has on now, all and by kind (Jellyfin's Live TV sections, as Wholphin
        shows them); a kind with nothing on is left out."""
        kinds = (("On now", ""), ("Sports", "isSports"), ("News", "isNews"), ("Kids", "isKids"), ("Movies", "isMovie"))

        def row(kind: str) -> list[dict]:
            try:
                return [
                    programme(p)
                    for p in jf.airing(kind)
                    if p.get("ChannelId") and not FILLER.match(p.get("Name") or "")
                ]
            except JellyfinError:
                return []

        with ThreadPoolExecutor(max_workers=len(kinds)) as pool:  # each a Jellyfin call of a second or two
            found = list(pool.map(row, [k for _, k in kinds]))
        return {"rows": [{"name": name, "items": items} for (name, _), items in zip(kinds, found) if items]}

    @api.post("/api/jf/watched")
    def watched(body: Watched):
        """Watched, or not: Jellyfin's played mark (every Jellyfin app shows it)."""
        guard(jf.set_played, body.id, body.on)
        return {"played": body.on}

    @api.get("/api/jf/people")
    def people(q: str = Query(max_length=200)):
        """Cast and crew whose name has every word of the search (as Wholphin's search finds them), with how many of
        their titles the library has; people with none are left out, most titles first."""
        if not any(len(w) > 1 for w in words(q)):
            return []
        found = [p for p in guard(jf.people, q) if named(q, p)][:8]
        out = [
            {"id": p["Id"], "name": p["Name"], "thumb": card(p)["thumb"], "count": guard(jf.count_with, p["Id"])}
            for p in found
        ]
        return sorted((p for p in out if p["count"]), key=lambda p: -p["count"])

    @api.get("/api/jf/person/{id}")
    def person(id: str = Path(pattern=ID)):
        """A person's page: their movies and shows in the library, newest first."""
        p = guard(jf.item, id)
        return {
            "id": id,
            "name": p.get("Name", ""),
            "thumb": card(p)["thumb"],
            "items": [card(i) for i in guard(jf.titles_with, id)],
        }

    @api.get("/api/jf/title")
    def title_page(id: str = Query(pattern=ID)):
        """A movie's page: its details, and the library's titles like it (empty when Jellyfin cannot say)."""
        i = guard(jf.details, id)
        try:
            more = [card(x) for x in jf.similar(id) if x.get("Type") in PLAYABLE | {"Series"}]
        except JellyfinError:
            more = []
        return details_of(i) | {"more": more}

    @api.get("/api/jf/show/{id}")
    def show_page(id: str = Path(pattern=ID)):
        """A show's page, as Wholphin's: its details, seasons, the next episode to watch (its main button), and every
        episode with its watch state."""
        i = guard(jf.details, id)
        try:
            nxt = jf.next_of(id)
        except JellyfinError:
            nxt = None
        episodes = [
            card(e)
            | {
                "season": e.get("ParentIndexNumber") or 0,
                "n": e.get("IndexNumber") or 0,
                "added": e.get("DateCreated") or "",
            }
            for e in guard(jf.episodes, id)
        ]
        seasons = [
            {"id": x["Id"], "name": x.get("Name", ""), "n": x.get("IndexNumber") or 0} for x in guard(jf.seasons, id)
        ]
        return details_of(i) | {"seasons": seasons, "next": card(nxt) if nxt else None, "episodes": episodes}

    def details_of(i: dict) -> dict:
        """A title's details for its page: facts, genres, cast and crew (each opening their page), its backdrop."""
        id = i["Id"]
        people = i.get("People") or []
        return card(i) | {
            "year": i.get("ProductionYear") or "",
            "minutes": round(i["RunTimeTicks"] / 6e8) if i.get("RunTimeTicks") else 0,
            "rating": i.get("OfficialRating") or "",
            "score": round(i["CommunityRating"], 1) if i.get("CommunityRating") else "",
            "genres": i.get("Genres") or [],
            "cast": [p["Name"] for p in people if p.get("Type") == "Actor"][:8],
            "directors": [p["Name"] for p in people if p.get("Type") == "Director"],
            # Each opens its own page (/api/jf/person/<id>): the cast first, then the directors.
            "people": [
                {
                    "id": p.get("Id") or "",
                    "name": p["Name"],
                    "role": p["Type"],
                    "as": p.get("Role") or "",
                    "thumb": f"/jf/Items/{p['Id']}/Images/Primary?maxHeight=240&quality=85"
                    if p.get("Id") and p.get("PrimaryImageTag")
                    else "",
                }
                for p in [x for x in people if x.get("Type") == "Actor"][:8]
                + [x for x in people if x.get("Type") == "Director"]
            ],
            "backdrop": f"/jf/Items/{id}/Images/Backdrop?maxWidth=1280&quality=85"
            if i.get("BackdropImageTags")
            else "",
        }

    @api.get("/api/jf/home")
    def home_rows():
        """Home's own rows from Jellyfin: Continue watching, Next up, New episodes (a row that fails is empty)."""

        def rows(fn, *args):
            try:
                return [card(i) for i in fn(*args)]
            except JellyfinError:
                return []

        return {"resume": rows(jf.resume), "next_up": rows(jf.next_up), "new": rows(jf.recent_episodes, 7)}

    @api.get("/api/jf/now")
    def now_playing(tv: str = Query("", max_length=100)):
        """What the Jellyfin TV app is playing on a TV (the first one when none is named): {} when nothing, or the app
        is closed."""
        host = which_tv(tvs, tv) or (tvs.host() if tvs else "")
        session = guard(jf.tv_playing, host)
        item = (session or {}).get("NowPlayingItem")
        if not item:
            return {}
        state = session.get("PlayState") or {}
        ep = (
            f"S{item.get('ParentIndexNumber') or 0:02d}E{item.get('IndexNumber') or 0:02d}"
            if item.get("IndexNumber")
            else ""
        )
        return {
            "title": item.get("Name", ""),
            "show": item.get("SeriesName", ""),
            "episode": ep,
            "paused": bool(state.get("IsPaused")),
            "minute": round((state.get("PositionTicks") or 0) / 6e8),
            "of_minutes": round((item.get("RunTimeTicks") or 0) / 6e8),
            "jellyfin_id": item.get("Id", ""),
        }

    @api.get("/api/jf/new")
    def new_episodes(days: int = 7):
        """Episodes Jellyfin added in the last few days, newest first."""
        return [
            {
                "id": i["Id"],
                "show": i.get("SeriesName", ""),
                "episode": f"S{i.get('ParentIndexNumber') or 0:02d}E{i.get('IndexNumber') or 0:02d}",
                "title": i.get("Name", ""),
                "added": (i.get("DateCreated") or "")[:10],
            }
            for i in guard(jf.recent_episodes, max(1, min(days, 30)))
        ]

    @api.get("/api/jf/play/{id}")
    def play(id: str):
        """Play here's video source: a link the page sets at once, inside the tap (iOS starts a video only from a
        tap, so the page cannot wait for an answer first); this sends it on to the file or to Jellyfin's HLS."""
        item = guard(jf.item, id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        return RedirectResponse(play_source(item), 302)

    @api.get("/api/jf/app")
    def app_ids():
        """What the iPhone's Jellyfin app needs to open an item: the app (Settings: phone_app), and Swiftfin's ids."""
        return guard(jf.app_ids) | {"app": phone_app()}

    @api.get("/api/jf/resume/{id}")
    def resume_at(id: str = Path(pattern=ID)):
        """Where Play here starts: where it was left, on any player (seconds)."""
        return {"start": (guard(jf.user_data, id).get("PlaybackPositionTicks") or 0) / 1e7}

    @api.delete("/api/jf/resume/{id}", status_code=204)
    def forget_resume(id: str = Path(pattern=ID)):
        """Off Continue watching, in every Jellyfin app: Jellyfin forgets where it was left (watched stays as it is)."""
        guard(jf.clear_position, id)

    @api.post("/api/jf/playing", status_code=204)
    def watching(w: Watching):
        """Play here's progress, into Jellyfin: Continue watching and played follow it everywhere, the TV too."""
        ticks = int(w.position * 1e7)
        event = {"start": "", "progress": "Progress", "stopped": "Stopped"}[w.event]
        body = {"ItemId": w.id, "PlaySessionId": w.session, "PositionTicks": ticks, "IsPaused": w.paused}
        try:
            jf.report(event, body | {"CanSeek": True} if w.event == "start" else body)
        except JellyfinError:
            pass  # only the "now playing" line in Jellyfin's dashboard; the user's data below is what counts
        if w.event == "start":
            return
        length = guard(jf.item, w.id).get("RunTimeTicks") or int(w.duration * 1e7)
        done = ticks / length if length else 0
        if w.event == "stopped" and done >= MAX_RESUME:
            guard(jf.mark_played, w.id)
        elif done >= MIN_RESUME or (not length and ticks):
            guard(jf.save_position, w.id, ticks)

    @api.get("/api/jf/next/{id}")
    def next_episode(id: str):
        """The episode Play here goes on to at the end ({} after the last one)."""
        n = guard(jf.next_episode, guard(jf.item, id))
        return card(n) if n else {}

    # My list is the user's Jellyfin favourites: one list for every Jellyfin app (mylist.py).
    def live(i: dict) -> bool:
        """A provider's live channel (Jellyfin's own Live TV channels stay favourites: its guide reads them)."""
        path = i.get("Path") or ""
        return any(path.startswith(root.rstrip("/") + "/") for root in live_roots())

    def live_list() -> tuple[str, list[dict]]:
        pid = guard(jf.playlist, LIVE_LIST)
        return pid, guard(jf.playlist_items, pid) if pid else []

    twins: dict[str, tuple[float, dict | None]] = {}  # a provider live video -> (when looked, its Live TV channel)

    def twin(i: dict) -> dict | None:
        """The Live TV channel (Jellyfin's tuner, with the guide) of a provider's live video: the same name. Looked up
        again after a day, so a channel the tuner adds later is found."""
        seen = twins.get(i["Id"])
        if seen and time.time() - seen[0] < 86400:
            return seen[1]
        key = re.sub(r"[^a-z0-9]", "", (i.get("Name") or "").lower())
        try:
            found = next(
                (c for c in jf.channels_named(i["Name"]) if re.sub(r"[^a-z0-9]", "", c["Name"].lower()) == key), None
            )
        except JellyfinError:
            return None  # asked again next time
        twins[i["Id"]] = (time.time(), found)
        return found

    def tidy(pid: str, left: list[dict]) -> None:
        """The Live channels list, empty: gone (no empty row in Wholphin)."""
        if pid and not left:
            guard(jf.delete_item, pid)

    @api.get("/api/jf/list")
    def listed():
        """Newest on My list first (added: when, in seconds; live: a live channel). A provider's live channel is its
        Live TV channel, a favourite there (apps show favourite channels first in Live TV and its guide): one put
        on the Live channels list earlier, or made a favourite in an app as a video, moves there here; one with no
        Live TV channel stays on (or goes to) the list."""
        found = {i["Id"]: i for i in guard(jf.favourites)}
        pid, channels = live_list()
        for i in [x for x in found.values() if live(x)]:  # a video favourited in an app
            guard(jf.set_favourite, i["Id"], False)
            del found[i["Id"]]
            t = twin(i)
            if t:
                guard(jf.set_favourite, t["Id"], True)
                found[t["Id"]] = t
            elif i["Id"] not in {c["Id"] for c in channels}:
                guard(jf.add_to_playlist, LIVE_LIST, i["Id"])
                pid, channels = live_list()
        moved = [(c, t) for c in channels if (t := twin(c))]
        if moved:
            guard(jf.remove_from_playlist, pid, [c["PlaylistItemId"] for c, _ in moved])
            for _, t in moved:
                guard(jf.set_favourite, t["Id"], True)
                found[t["Id"]] = t
            channels = [c for c in channels if c["Id"] not in {m["Id"] for m, _ in moved}]
            tidy(pid, channels)
        everything = {i["Id"]: (i, i.get("Type") == "TvChannel") for i in found.values()} | {
            c["Id"]: (c, True) for c in channels
        }
        library = {i: since(x.get("DateCreated")) for i, (x, _) in everything.items()}
        added = dates.added(library) if dates else library
        cards = (card(x) | {"added": added[i], "live": on} for i, (x, on) in everything.items())
        return sorted(cards, key=lambda c: -c["added"])

    @api.post("/api/jf/list", status_code=201)
    def add_to_list(body: Play):
        chans["at"] = 0.0  # a channel's ♥: Live TV's order changes
        item = guard(jf.item, body.id)
        found = card(item)
        t = twin(item) if live(item) else None
        if t:  # its Live TV channel, a favourite
            if guard(jf.user_data, t["Id"]).get("IsFavorite"):
                return {"added": [], "already": [found["title"]]}
            guard(jf.set_favourite, t["Id"], True)
        elif live(item):
            if any(c["Id"] == body.id for c in live_list()[1]):
                return {"added": [], "already": [found["title"]]}
            guard(jf.add_to_playlist, LIVE_LIST, body.id)
        elif guard(jf.user_data, body.id).get("IsFavorite"):
            return {"added": [], "already": [found["title"]]}
        else:
            guard(jf.set_favourite, body.id, True)
        if dates:
            dates.favourite(body.id, True)
        return {"added": [found["title"]], "already": []}

    @api.delete("/api/jf/list", status_code=204)
    def remove_from_list(id: str = Query(pattern=ID)):
        chans["at"] = 0.0
        pid, channels = live_list()
        entries = [c["PlaylistItemId"] for c in channels if c["Id"] == id]
        if entries:
            guard(jf.remove_from_playlist, pid, entries)
            tidy(pid, [c for c in channels if c["Id"] != id])
        elif guard(jf.user_data, id).get("IsFavorite"):
            guard(jf.set_favourite, id, False)
        else:
            raise HTTPException(404, "not on My list")
        if dates:
            dates.favourite(id, False)

    @api.post("/api/jf/tv")
    def on_tv(p: Play):
        """Starts it in the Jellyfin TV app."""
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        host = which_tv(tvs, p.tv)
        guard(lambda: jf.play_on_tv(p.id, aimed(open_app, host), play_app=aimed(play_app, host), host=host))
        return {"playing": item.get("Name")}

    @api.post("/api/jf/phone")
    def on_phone(p: Play):
        """▶ Jellyfin on the iPhone: the page opens Swiftfin at the item, and this starts it playing there."""
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        guard(jf.play_on_phone, p.id)
        return {"playing": item.get("Name")}

    @api.post("/api/jf/kodi")
    def on_kodi(p: Play):
        """Kodi on the TV, which fetches the file through the LAN resolver (the API key stays on the Air)."""
        if not kodi:
            raise HTTPException(501, "Kodi is not set up")
        item = guard(jf.item, p.id)
        if item.get("Type") not in PLAYABLE:
            raise HTTPException(400, "not a video")
        try:
            kodi.rpc("Player.Open", {"item": {"file": f"{tv_base}/jf/{p.id}"}})
        except KodiError as e:
            raise HTTPException(502, str(e)) from e
        return {"playing": item.get("Name")}

    @api.get("/jf/{path:path}")
    def passthrough(path: str, request: Request):
        """Video bytes, HLS playlists and segments, and images, with the API key added here."""
        if not PASSABLE.fullmatch(path):
            raise HTTPException(404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        r = guard(jf.raw, "/" + path, dict(request.query_params), headers)
        out = {k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
        return StreamingResponse(
            r.iter_bytes(1 << 16), status_code=r.status_code, headers=out, background=BackgroundTask(r.close)
        )

    return api
