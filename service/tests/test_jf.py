import json
import re
import time

import httpx
from fastapi.testclient import TestClient

from mimir_tv.app import create_app
from mimir_tv.jellyfin import Jellyfin
from mimir_tv.jf_routes import router
from mimir_tv.kodi import Kodi
from mimir_tv.mylist import ListDates
from mimir_tv.resolver import start_resolver
from mimir_tv.tv import Tvs

MOVIE = "a" * 32
WEDDING = "b" * 32
HOME = "c" * 32
CLIP = "d" * 32
BYTES = bytes(range(256)) * 40


def item(id_, name, type_, container="mp4", video="h264", audio="aac", **kw):
    return {
        "Id": id_,
        "Name": name,
        "Type": type_,
        "IsFolder": type_ in ("CollectionFolder", "Folder"),
        "RunTimeTicks": 36_000_000_000 if type_ != "CollectionFolder" else None,
        "ImageTags": {"Primary": "x"},
        "MediaSources": [
            {
                "Id": id_,
                "Container": container,
                "MediaStreams": [{"Type": "Video", "Codec": video}, {"Type": "Audio", "Codec": audio}],
            }
        ],
        **kw,
    }


SHOW, EP1, EP2 = "3" * 32, "1" * 32, "2" * 32
PRABHAS, DEEPIKA, NAMESAKE = "f1" * 16, "f2" * 16, "f3" * 16  # people: actors with titles, and one with none here
PERSONS = {
    PRABHAS: {"Id": PRABHAS, "Name": "Prabhas", "Type": "Person", "ImageTags": {"Primary": "p"}},
    DEEPIKA: {"Id": DEEPIKA, "Name": "Deepika Padukone", "Type": "Person", "ImageTags": {}},
    NAMESAKE: {"Id": NAMESAKE, "Name": "Prabhas Sreenu", "Type": "Person", "ImageTags": {}},
}
FILM, LIKE_IT = "4" * 32, "5" * 32
ITEMS = {
    MOVIE: item(MOVIE, "Wedding Film 4K", "Video"),
    EP1: item(EP1, "Pilot", "Episode", SeriesId=SHOW, SeriesName="A Show", ParentIndexNumber=1, IndexNumber=1),
    EP2: item(EP2, "Second", "Episode", SeriesId=SHOW, SeriesName="A Show", ParentIndexNumber=1, IndexNumber=2),
    CLIP: item(CLIP, "Old clip", "Video", container="avi", video="mpeg4", audio="mp3"),
    HOME: item(HOME, "Home Videos", "CollectionFolder"),
    "9" * 32: item("9" * 32, "NDTV 24x7", "TvChannel"),  # a live channel from an IPTV provider
    FILM: item(
        FILM,
        "Kalki 2898 AD",
        "Movie",
        ProductionYear=2024,
        OfficialRating="UA",
        CommunityRating=7.46,
        Genres=["Science Fiction", "Action"],
        People=[
            {"Name": "Prabhas", "Type": "Actor", "Id": PRABHAS, "Role": "Bhairava", "PrimaryImageTag": "p"},
            {"Name": "Deepika Padukone", "Type": "Actor", "Id": DEEPIKA, "Role": "Sumathi"},
            {"Name": "Nag Ashwin", "Type": "Director"},
            {"Name": "C. Ashwini Dutt", "Type": "Producer"},
        ],
        BackdropImageTags=["b"],
    ),
    LIKE_IT: item(
        LIKE_IT, "Salaar", "Movie", ProductionYear=2023, People=[{"Name": "Prabhas", "Type": "Actor", "Id": PRABHAS}]
    ),
}


EPISODES: list[dict] = []  # newest first, as Jellyfin sorts them
NEXT_UP: list[dict] | None = []  # None: Jellyfin fails that call
SESSIONS: list[dict] = []
PLAYED: list[tuple] = []
TV_APP = {"Id": "tv", "Client": "Jellyfin Android TV", "SupportsRemoteControl": True, "PlayableMediaTypes": ["Video"]}
USER_DATA: dict[str, dict] = {}  # the admin's data per item: position, played, favourite
REPORTS: list[tuple] = []  # session reports: (event, body)
TUNER_M3U = "#EXTM3U\n"  # the playlist Live TV's tuner reads (its groups: Jellyfin keeps none)
SEARCHED: list[str] = []  # each search Jellyfin was asked (its term)
PROGRAMMES: list[dict] = []  # the TV guide's programmes (Jellyfin's Live TV, from an XMLTV source)
PLAYLISTS: dict[str, dict] = {}  # the user's playlists: id -> {"Name", "items": [item ids, in order]}
LIVE_ROOT = "/media/tv1-live"  # a provider's live channels (.strm files)
PHONE_APP = {"Id": "phone", "Client": "Jellyfin iOS", "SupportsRemoteControl": True, "PlayableMediaTypes": ["Video"]}


def fake(request: httpx.Request) -> httpx.Response:
    p, q = request.url.path, request.url.params
    if p == "/Items" and q.get("IncludeItemTypes") == "Playlist":
        assert q["userId"] == "u1"
        return httpx.Response(
            200, json={"Items": [{"Id": k, "Name": v["Name"], "Type": "Playlist"} for k, v in PLAYLISTS.items()]}
        )
    if p == "/Playlists" and request.method == "POST":
        body = json.loads(request.content)
        pid = f"pl{len(PLAYLISTS) + 1}"
        PLAYLISTS[pid] = {"Name": body["Name"], "items": list(body["Ids"])}
        return httpx.Response(200, json={"Id": pid})
    if p.startswith("/Items/") and request.method == "DELETE" and p.split("/")[2] in PLAYLISTS:
        del PLAYLISTS[p.split("/")[2]]
        return httpx.Response(204)
    if p.startswith("/Playlists/") and p.endswith("/Items"):
        pl = PLAYLISTS[p.split("/")[2]]
        if request.method == "POST":
            pl["items"] += [i for i in q["Ids"].split(",") if i]
            return httpx.Response(204)
        if request.method == "DELETE":  # entries are named by their place in the list (PlaylistItemId)
            gone = set(q["EntryIds"].split(","))
            pl["items"] = [i for n, i in enumerate(pl["items"]) if f"entry{n}" not in gone]
            return httpx.Response(204)
        return httpx.Response(
            200, json={"Items": [ITEMS[i] | {"PlaylistItemId": f"entry{n}"} for n, i in enumerate(pl["items"])]}
        )
    if p == "/Library/MediaFolders":
        return httpx.Response(
            200,
            json={
                "Items": [
                    item(WEDDING, "Wedding", "CollectionFolder"),
                    ITEMS[HOME],
                    item("e" * 32, "My IPTV", "CollectionFolder"),
                ]
            },
        )
    if p == f"/Items/{FILM}/Similar":
        return httpx.Response(200, json={"Items": [ITEMS[LIKE_IT], ITEMS["9" * 32]]})  # a channel is no movie
    if p.endswith("/Similar"):
        return httpx.Response(500, text="boom")
    if p == "/Items" and q.get("IncludeItemTypes") == "LiveTvProgram":  # loose too: any word, in the name
        assert q["userId"] == "u1" and q["MinEndDate"] and q["MaxStartDate"]
        words = q["SearchTerm"].lower().split()
        hits = [x for x in PROGRAMMES if any(w in x["Name"].lower() for w in words)]
        hits = [x for x in hits if x["EndDate"] > q["MinEndDate"] and x["StartDate"] < q["MaxStartDate"]]
        return httpx.Response(200, json={"Items": hits})
    if p == "/LiveTv/Channels":  # Live TV's channels, favourites first when asked
        assert q["userId"] == "u1"
        chans = [i | {"UserData": USER_DATA.get(i["Id"], {})} for i in ITEMS.values() if i["Type"] == "TvChannel"]
        chans.sort(key=lambda c: int(c.get("ChannelNumber") or 0))
        if q.get("enableFavoriteSorting") == "true":
            chans.sort(key=lambda c: not c["UserData"].get("IsFavorite"))
        return httpx.Response(200, json={"Items": chans, "TotalRecordCount": len(chans)})
    if p == "/System/Configuration/livetv":
        return httpx.Response(
            200, json={"TunerHosts": [{"Type": "m3u", "Url": "http://tuner.example/get.php?username=u"}]}
        )
    if p == "/LiveTv/Programs" and "channelIds" in q:  # a guide's window for some channels
        ids = q["channelIds"].split(",")
        hits = [
            x
            for x in PROGRAMMES
            if x["ChannelId"] in ids and x["EndDate"] > q["minEndDate"] and x["StartDate"] < q["maxStartDate"]
        ]
        return httpx.Response(200, json={"Items": sorted(hits, key=lambda x: x["StartDate"])})
    if p == "/LiveTv/Programs":  # what is airing, by kind
        assert q["userId"] == "u1" and q["isAiring"] == "true"
        kind = next((k for k in ("isSports", "isNews", "isKids", "isMovie") if q.get(k) == "true"), None)
        hits = [x for x in PROGRAMMES if not kind or x.get(kind[0].upper() + kind[1:])]
        return httpx.Response(200, json={"Items": hits[: int(q["limit"])]})
    if p == "/Persons":  # loose, as Jellyfin's search is: any one word
        assert q["userId"] == "u1"
        words = q["searchTerm"].lower().split()
        return httpx.Response(
            200, json={"Items": [x for x in PERSONS.values() if any(w in x["Name"].lower() for w in words)]}
        )
    if p == "/Items" and "PersonIds" in q:
        kinds = q["IncludeItemTypes"].split(",")
        hits = [
            i
            for i in ITEMS.values()
            if i["Type"] in kinds and any(x.get("Id") == q["PersonIds"] for x in i.get("People") or [])
        ]
        hits.sort(key=lambda i: -(i.get("ProductionYear") or 0))
        return httpx.Response(200, json={"Items": [] if q.get("Limit") == "0" else hits, "TotalRecordCount": len(hits)})
    if p == "/Items" and "Ids" in q:
        found = ITEMS | PERSONS  # Jellyfin gives a person by id like any item
        return httpx.Response(200, json={"Items": [found[q["Ids"]]] if q["Ids"] in found else []})
    if p == "/Items" and q.get("SortBy") == "DateCreated":  # a show's newest episodes
        return httpx.Response(200, json={"Items": [e for e in EPISODES if e.get("SeriesId") == q.get("ParentId")]})
    if p == "/Items" and "ParentId" in q:
        return httpx.Response(200, json={"Items": [ITEMS[MOVIE], ITEMS[CLIP]] if q["ParentId"] == HOME else []})
    if p == "/UserItems/Resume":
        assert q["userId"] == "u1"
        return httpx.Response(
            200, json={"Items": [ITEMS[MOVIE] | {"UserData": {"PlaybackPositionTicks": 9_000_000_000}}]}
        )
    if p == "/Shows/NextUp":
        return httpx.Response(500, text="boom") if NEXT_UP is None else httpx.Response(200, json={"Items": NEXT_UP})
    if p == "/Items" and "SearchTerm" in q:  # loose, as Jellyfin's is: any one word of the search is enough
        SEARCHED.append(q["SearchTerm"])
        words = [w for w in re.split(r"\W+", q["SearchTerm"].lower()) if len(w) > 1]
        named = lambda i: f"{i['Name']} {i.get('SeriesName', '')}".lower()  # noqa: E731
        # Like Jellyfin: asked without a user, Live TV's channels are left out.
        found = [i for i in ITEMS.values() if any(w in named(i) for w in words)]
        found = [i for i in found if q.get("userId") == "u1" or i["Type"] != "TvChannel"]
        kinds = q["IncludeItemTypes"].split(",") if q.get("IncludeItemTypes") else None  # one kind a call, as Wholphin
        return httpx.Response(200, json={"Items": [i for i in found if not kinds or i["Type"] in kinds]})
    if p == f"/Videos/{MOVIE}/stream":
        assert "Token=" in request.headers["Authorization"]
        rng = request.headers.get("Range")
        if rng:
            a, b = (int(x) for x in rng.split("=")[1].split("-"))
            return httpx.Response(
                206,
                content=BYTES[a : b + 1],
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {a}-{b}/{len(BYTES)}",
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(200, content=BYTES, headers={"Content-Type": "video/mp4"})
    if p == "/System/Info/Public":
        return httpx.Response(200, json={"Id": "srv1", "ServerName": "air"})
    if p == "/Users":
        return httpx.Response(
            200,
            json=[
                {"Id": "u2", "Policy": {"IsAdministrator": False}},
                {"Id": "u1", "Policy": {"IsAdministrator": True}},
            ],
        )
    if p.startswith("/Sessions/Playing"):
        REPORTS.append((p.removeprefix("/Sessions/Playing").strip("/"), json.loads(request.content)))
        return httpx.Response(204)
    user_item = p.split("/")[2] if p.count("/") >= 2 else ""
    if p.startswith(("/UserItems/", "/UserPlayedItems/", "/UserFavoriteItems/")):
        assert q["userId"] == "u1"
        if user_item not in ITEMS:
            return httpx.Response(404)
        data = USER_DATA.setdefault(user_item, {"PlaybackPositionTicks": 0, "Played": False, "PlayCount": 0})
        if p.endswith("/UserData") and request.method == "POST":
            data.update(json.loads(request.content))
        elif p.startswith("/UserPlayedItems/") and request.method == "DELETE":
            data.update(Played=False)
        elif p.startswith("/UserPlayedItems/"):
            data.update(Played=True, PlayCount=data["PlayCount"] + 1, PlaybackPositionTicks=0)
        elif p.startswith("/UserFavoriteItems/"):
            data["IsFavorite"] = request.method == "POST"
        return httpx.Response(200, json=data)
    if p == "/Items" and q.get("Filters") == "IsFavorite":
        return httpx.Response(200, json={"Items": [ITEMS[i] for i, d in USER_DATA.items() if d.get("IsFavorite")]})
    if p == f"/Shows/{SHOW}/Episodes":
        eps = [ITEMS[EP1], ITEMS[EP2]]
        if "startItemId" not in q:  # the whole show, with each episode's watch state, as a user sees it
            assert q["userId"] == "u1"
            return httpx.Response(200, json={"Items": [e | {"UserData": USER_DATA.get(e["Id"], {})} for e in eps]})
        at = next(n for n, e in enumerate(eps) if e["Id"] == q["startItemId"])
        return httpx.Response(200, json={"Items": eps[at : at + int(q["limit"])]})
    if p == f"/Shows/{SHOW}/Seasons":
        return httpx.Response(
            200, json={"Items": [{"Id": "9a" * 16, "Name": "Season 1", "IndexNumber": 1, "Type": "Season"}]}
        )
    if p == "/Sessions":  # like Jellyfin: asked for sessions a user can command, only those taking remote control
        if "ControllableByUserId" in q:
            assert q["ControllableByUserId"] == "u1"
            return httpx.Response(200, json=[x for x in SESSIONS if x.get("SupportsRemoteControl")])
        return httpx.Response(200, json=SESSIONS)
    if p.startswith("/Sessions/") and "/Playing" in p:
        session = next(x for x in SESSIONS if x["Id"] == p.split("/")[2])
        if p.endswith("/Playing/Stop"):
            session.pop("NowPlayingItem", None)
            return httpx.Response(204)
        PLAYED.append((session["Id"], dict(q)))
        # Like the TV app: "play" while something plays only stops it.
        if not session.pop("NowPlayingItem", None):
            session["NowPlayingItem"] = {"Id": q["itemIds"]}
        return httpx.Response(204)
    if p.endswith("/Images/Primary"):
        return httpx.Response(200, content=b"jpg", headers={"Content-Type": "image/jpeg"})
    if p == "/Auth/Keys":  # what the passthrough must never reach: every API key
        return httpx.Response(200, json={"Items": [{"AccessToken": "admin-key"}]})
    return httpx.Response(404)


def setup(tmp_path):
    USER_DATA.clear()
    PLAYLISTS.clear()
    REPORTS.clear()
    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    sent = []
    kodi = Kodi(
        "http://tv",
        "pw",
        transport=httpx.MockTransport(
            lambda r: (
                sent.append(r.read().decode()) or httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "OK"})
            )
        ),
    )
    c = TestClient(
        create_app(
            jf,
            tmp_path / "picks",
            router(
                jf,
                kodi,
                "https://lan.example",
                dates=ListDates(tmp_path / "m.db"),
                hidden=lambda: {"My IPTV"},
                live_roots=lambda: [LIVE_ROOT],
                fetch=lambda url: TUNER_M3U if url.startswith("http://tuner.example/") else "",
            ),
        )
    )
    return jf, c, sent


def test_browse_search_and_pick_how_to_play(tmp_path):
    _, c, _ = setup(tmp_path)
    root = c.get("/api/jf/browse").json()
    assert [i["title"] for i in root["items"]] == ["Wedding", "Home Videos"]  # a provider's library: in its own section
    home = c.get("/api/jf/browse", params={"id": HOME}).json()
    assert [(i["title"], i["kind"], i["sub"]) for i in home["items"]] == [
        ("Wedding Film 4K", "video", "60 min"),
        ("Old clip", "video", "60 min"),
    ]
    assert [i["title"] for i in c.get("/api/jf/search", params={"q": "wedding"}).json()] == ["Wedding Film 4K"]
    # Jellyfin's loose matches stay out: every word must be in the name (or the show's name), in any order.
    search = lambda q: [i["title"] for i in c.get("/api/jf/search", params={"q": q}).json()]  # noqa: E731
    assert search("Pilot: A Show!") == ["Pilot"]
    assert search("old wedding") == []
    found = c.get("/api/jf/search", params={"q": "ndtv"}).json()
    assert [(i["title"], i["kind"], i["sub"]) for i in found] == [("NDTV 24x7", "video", "Live TV")]

    # Safari plays MP4/H.264 as is; an AVI/MPEG-4 file goes through Jellyfin's HLS conversion.
    src = c.get(f"/api/jf/play/{MOVIE}", follow_redirects=False)  # set by the page inside the tap, then sent on
    assert src.status_code == 302 and src.headers["location"].startswith(f"/jf/Videos/{MOVIE}/stream?static=true")
    assert "/master.m3u8?" in c.get(f"/api/jf/play/{CLIP}", follow_redirects=False).headers["location"]
    assert c.get(f"/api/jf/play/{HOME}").status_code == 400


def test_passthrough_streams_ranges_and_keeps_the_key_on_the_server(tmp_path):
    _, c, _ = setup(tmp_path)
    r = c.get(f"/jf/Videos/{MOVIE}/stream", params={"static": "true"}, headers={"Range": "bytes=100-199"})
    assert (
        r.status_code == 206
        and r.content == BYTES[100:200]
        and r.headers["content-range"] == f"bytes 100-199/{len(BYTES)}"
    )
    assert c.get(f"/jf/Items/{MOVIE}/Images/Primary").content == b"jpg"
    # Only videos and images pass through; nothing else of Jellyfin's API.
    assert c.get("/jf/Library/VirtualFolders").status_code == 404
    assert c.get("/jf/Users").status_code == 404
    # Nor by climbing out of an allowed path: dots (encoded or not) never reach Jellyfin, which would answer.
    for up in (
        "Videos/%2e%2e/Auth/Keys",
        "Videos/../Auth/Keys",
        f"Items/{MOVIE}/Images/%2e%2e/%2e%2e/%2e%2e/Auth/Keys",
        f"Videos/{MOVIE}/%2E%2E/%2E%2E/Auth/Keys",
        f"Videos/{MOVIE}/..%2f..%2fAuth/Keys",
    ):
        r = c.get("/jf/" + up)
        assert r.status_code == 404 and b"admin-key" not in r.content, up


def test_the_iphones_jellyfin_app_and_ids_for_its_links(tmp_path):
    _, c, _ = setup(tmp_path)
    # Swiftfin unless Settings say otherwise; its links need the server and the administrator (not the first user).
    assert c.get("/api/jf/app").json() == {"server": "srv1", "user": "u1", "app": "swiftfin"}
    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    streamy = TestClient(
        create_app(jf, tmp_path / "p2", router(jf, None, "https://lan.example", phone_app=lambda: "streamyfin"))
    )
    assert streamy.get("/api/jf/app", headers={"Host": "127.0.0.1"}).json()["app"] == "streamyfin"


def test_tv_plays_in_jellyfins_tv_app_not_the_phone(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP, dict(TV_APP)])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    slept = []
    monkeypatch.setattr("mimir_tv.jellyfin.time.sleep", slept.append)
    _, c, sent = setup(tmp_path)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert slept == []  # the app was already open: no waiting
    assert PLAYED == [("tv", {"playCommand": "PlayNow", "itemIds": MOVIE})]
    assert sent == []  # not Kodi


def test_tv_plays_in_wholphin_the_jellyfin_tv_app_in_use(tmp_path, monkeypatch):
    wholphin = TV_APP | {"Id": "wholphin", "Client": "Wholphin"}
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP, wholphin])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert PLAYED == [("wholphin", {"playCommand": "PlayNow", "itemIds": MOVIE})]


def test_tv_switches_from_what_it_is_playing_in_one_press(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [TV_APP | {"NowPlayingItem": {"Id": CLIP}}])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    monkeypatch.setattr("mimir_tv.jellyfin.time.sleep", lambda s: None)
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert SESSIONS[0]["NowPlayingItem"] == {"Id": MOVIE}


def test_tv_opens_the_jellyfin_app_when_it_is_closed_and_waits_for_it(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    opened = []

    def open_app():  # what the ADB call does: the app starts and connects to Jellyfin
        opened.append(1)
        SESSIONS.append(dict(TV_APP))

    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    c = TestClient(create_app(jf, tmp_path / "picks", router(jf, None, "https://lan.example", open_app)))
    slept = []
    monkeypatch.setattr("mimir_tv.jellyfin.time.sleep", slept.append)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert opened == [1] and [p[0] for p in PLAYED] == ["tv"]
    # The app connects before it is ready to play ("no playable items"): a moment to settle before the command.
    assert slept[-1] >= 2


def test_tv_brings_the_app_forward_when_it_was_left_in_the_background(tmp_path, monkeypatch):
    # Left for TV Home, the app keeps its Jellyfin session for minutes but cannot come forward by itself: a play
    # sent to it then shows nothing. It is brought to the front first; when it is in front already, no wait.
    monkeypatch.setattr("tests.test_jf.SESSIONS", [dict(TV_APP)])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    front = [False]
    calls = []

    def open_app():
        calls.append(1)
        was_front, front[0] = front[0], True
        return not was_front

    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    c = TestClient(create_app(jf, tmp_path / "picks", router(jf, None, "https://lan.example", open_app)))
    slept = []
    monkeypatch.setattr("mimir_tv.jellyfin.time.sleep", slept.append)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert calls == [1] and slept and slept[-1] >= 2 and [p[0] for p in PLAYED] == ["tv"]
    slept.clear()
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert calls == [1, 1] and all(s < 2 for s in slept) and len(PLAYED) == 2  # in front already: no settling


def test_tv_plays_in_wholphin_by_its_intent_from_where_it_was_left(tmp_path, monkeypatch):
    # Wholphin ignores Jellyfin's remote play command; it plays through its own intent, at the saved position.
    monkeypatch.setattr("tests.test_jf.SESSIONS", [dict(TV_APP)])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    monkeypatch.setitem(USER_DATA, MOVIE, {"PlaybackPositionTicks": 270_000 * 10_000})
    started, installed = [], [True]

    def play_app(item_id, ms):
        started.append((item_id, ms))
        return installed[0]

    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    c = TestClient(
        create_app(jf, tmp_path / "picks", router(jf, None, "https://lan.example", lambda: False, play_app=play_app))
    )
    monkeypatch.setattr("mimir_tv.jellyfin.time.sleep", lambda s: None)
    assert c.post("/api/jf/tv", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert started == [(MOVIE, 270_000)] and PLAYED == []  # 4 min 30 s in, and no remote command
    installed[0] = False  # no Wholphin: Jellyfin's own app gets the remote command
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert [p[0] for p in PLAYED] == ["tv"]


def test_a_play_goes_to_the_tv_it_names_and_each_tv_says_what_it_plays(tmp_path, monkeypatch):
    # Two TVs with Wholphin: the phone's TV menu names one; without a name the first TV plays, as with one TV.
    living = TV_APP | {"Id": "living", "Client": "Wholphin", "RemoteEndPoint": "192.0.2.20"}
    bedroom = TV_APP | {"Id": "bedroom", "Client": "Wholphin", "RemoteEndPoint": "192.0.2.21", "UserId": "u1"}
    bedroom["NowPlayingItem"] = {"Id": CLIP, "Name": "Clip", "Type": "Video", "RunTimeTicks": 6_000_000_000}
    monkeypatch.setattr("tests.test_jf.SESSIONS", [living, bedroom])
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    started = []
    tvs = Tvs(
        lambda: [{"name": "Living room", "host": "192.0.2.20"}, {"name": "Bedroom", "host": "192.0.2.21"}],
        tmp_path / "k",
    )
    jf = Jellyfin("http://jf", "secret-key", transport=httpx.MockTransport(fake))
    play = lambda item_id, ms, tv="": started.append((item_id, tv)) or True  # noqa: E731
    c = TestClient(create_app(jf, tmp_path / "picks", router(jf, None, "https://lan.example", play_app=play, tvs=tvs)))
    assert c.post("/api/jf/tv", json={"id": MOVIE, "tv": "bedroom"}).status_code == 200
    assert c.post("/api/jf/tv", json={"id": MOVIE}).status_code == 200
    assert started == [(MOVIE, "192.0.2.21"), (MOVIE, "")]  # by name (any case), then the first TV
    r = c.post("/api/jf/tv", json={"id": MOVIE, "tv": "Kitchen"})
    assert r.status_code == 404 and "Kitchen" in r.json()["detail"] and len(started) == 2
    assert c.get("/api/jf/now", params={"tv": "Bedroom"}).json()["title"] == "Clip"
    assert c.get("/api/jf/now").json() == {}  # the living room plays nothing, though the bedroom does


def test_tv_asks_for_the_jellyfin_app_when_it_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_jf.SESSIONS", [PHONE_APP])  # never the phone
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    _, c, _ = setup(tmp_path)
    r = c.post("/api/jf/tv", json={"id": MOVIE})
    assert r.status_code == 502 and r.json()["detail"] == "Open Jellyfin on the TV first" and PLAYED == []


def test_jellyfin_on_the_iphone_starts_playing_in_swiftfin(tmp_path, monkeypatch):
    # Swiftfin's links only open an item's page; the page opens one, and the Air sends the app PlayNow once it is up.
    swiftfin = {"Id": "iphone", "Client": "Swiftfin iOS", "SupportsRemoteControl": True}
    idle = swiftfin | {"Id": "ipad", "LastActivityDate": "2000-01-01T00:00:00.0000000Z"}  # open somewhere, untouched
    sessions = [PHONE_APP, idle]
    monkeypatch.setattr("tests.test_jf.SESSIONS", sessions)
    monkeypatch.setattr("tests.test_jf.PLAYED", [])
    now = time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())
    slept = []
    monkeypatch.setattr(
        "mimir_tv.jellyfin.time.sleep",
        lambda s: slept.append(s) or sessions.append(swiftfin | {"LastActivityDate": now}),
    )
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/phone", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert len(slept) == 1  # waited for the app to come up
    assert PLAYED == [("iphone", {"playCommand": "PlayNow", "itemIds": MOVIE})]  # not the idle iPad, not the TV
    assert c.post("/api/jf/phone", json={"id": HOME}).status_code == 400  # a folder only opens


def test_kodi_and_the_lan_stream_it_fetches(tmp_path):
    jf, c, sent = setup(tmp_path)
    assert c.post("/api/jf/kodi", json={"id": MOVIE}).json() == {"playing": "Wedding Film 4K"}
    assert f'"file":"https://lan.example/jf/{MOVIE}"' in sent[0] and "secret-key" not in sent[0]

    server = start_resolver(None, 0, "127.0.0.1", jf=jf)
    port = server.server_address[1]
    r = httpx.get(f"http://127.0.0.1:{port}/jf/{MOVIE}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == BYTES[:10]
    assert httpx.get(f"http://127.0.0.1:{port}/jf/not-an-id").status_code == 404
    server.shutdown()


def test_home_rows_continue_watching_next_up_and_new_episodes(tmp_path, monkeypatch):
    ep = {"Type": "Episode", "SeriesName": "The Traitors (Telugu)", "SeriesId": "s1", "SeriesPrimaryImageTag": "t"}
    monkeypatch.setattr(
        "tests.test_jf.EPISODES",
        [
            ep
            | {
                "Id": "e7",
                "Name": "Episode 7",
                "ParentIndexNumber": 1,
                "IndexNumber": 7,
                "DateCreated": "2099-01-01T00:00:00",
            },
            # A show nobody here watches (the whole catalogue adds many): not under New episodes.
            {
                "Type": "Episode",
                "SeriesName": "Other",
                "SeriesId": "s9",
                "Id": "x1",
                "DateCreated": "2099-01-02T00:00:00",
            },
        ],
    )
    monkeypatch.setattr(
        "tests.test_jf.NEXT_UP",
        [
            ep
            | {"Id": "e5", "Name": "Episode 5", "ParentIndexNumber": 1, "IndexNumber": 5, "ImageTags": {"Primary": "x"}}
        ],
    )
    monkeypatch.setitem(ITEMS, "s1", item("s1", "The Traitors (Telugu)", "Series"))
    _, c, _ = setup(tmp_path)
    rows = c.get("/api/jf/home").json()
    assert [(i["title"], i["progress"]) for i in rows["resume"]] == [("Wedding Film 4K", 25)]
    assert [(i["title"], i["sub"]) for i in rows["next_up"]] == [("Episode 5", "The Traitors (Telugu) · S01E05")]
    assert (rows["next_up"][0]["show"], rows["next_up"][0]["show_title"]) == ("jf:s1", "The Traitors (Telugu)")
    # Watched but not on My list (or taken off it): its new episodes stay out; Next up still has it.
    assert rows["new"] == []
    USER_DATA["s1"] = {"IsFavorite": True}
    rows = c.get("/api/jf/home").json()
    assert [i["title"] for i in rows["new"]] == ["Episode 7"]
    new = rows["new"][0]
    assert (new["sub"], new["thumb"]) == (
        "The Traitors (Telugu) · S01E07",
        "/jf/Items/s1/Images/Primary?maxHeight=480&quality=85",
    )
    # One row failing leaves the others.
    monkeypatch.setattr("tests.test_jf.NEXT_UP", None)
    rows = c.get("/api/jf/home").json()
    assert rows["next_up"] == [] and len(rows["resume"]) == 1


def test_my_list_titles_in_the_library_are_jellyfin_favourites(tmp_path):
    _, c, _ = setup(tmp_path)
    assert c.post("/api/jf/list", json={"id": MOVIE}).json() == {"added": ["Wedding Film 4K"], "already": []}
    assert USER_DATA[MOVIE]["IsFavorite"]  # every Jellyfin app shows it, and the recommendations read it
    assert c.post("/api/jf/list", json={"id": MOVIE}).json() == {"added": [], "already": ["Wedding Film 4K"]}
    listed = c.get("/api/jf/list").json()
    assert [(i["title"], i["id"], i["kind"]) for i in listed] == [("Wedding Film 4K", MOVIE, "video")]
    assert listed[0]["thumb"].startswith("/jf/Items/")  # shown and played through mimir-tv, as anywhere else
    USER_DATA[EP1] = {"IsFavorite": True}  # made a favourite in a Jellyfin app: on My list too
    assert {i["id"] for i in c.get("/api/jf/list").json()} == {MOVIE, EP1}
    assert c.delete("/api/jf/list", params={"id": MOVIE}).status_code == 204
    assert not USER_DATA[MOVIE]["IsFavorite"]
    assert c.delete("/api/jf/list", params={"id": MOVIE}).status_code == 404
    assert c.delete("/api/jf/list", params={"id": "../Users"}).status_code == 422


def test_live_channels_on_my_list_are_favourite_live_tv_channels(tmp_path, monkeypatch):
    news, sports, espn = "6" * 32, "7" * 32, "8" * 32
    news_tv, espn_tv = "8a" * 16, "8b" * 16
    monkeypatch.setitem(ITEMS, news, item(news, "News 24", "Video", Path=f"{LIVE_ROOT}/News 24/News 24.strm"))
    monkeypatch.setitem(ITEMS, sports, item(sports, "Sports 1", "Video", Path=f"{LIVE_ROOT}/Sports 1/Sports 1.strm"))
    monkeypatch.setitem(ITEMS, espn, item(espn, "ESPN 2 4K", "Video", Path=f"{LIVE_ROOT}/ESPN 2 4K/ESPN 2 4K.strm"))
    # Jellyfin's own Live TV has the same provider's channels (its tuner), with the TV guide.
    monkeypatch.setitem(ITEMS, news_tv, item(news_tv, "NEWS 24", "TvChannel"))
    monkeypatch.setitem(ITEMS, espn_tv, item(espn_tv, "ESPN 2 4K", "TvChannel"))
    _, c, _ = setup(tmp_path)
    PLAYLISTS["pl1"] = {"Name": "Live channels", "items": [espn]}  # put on the list before Live TV favourites
    # A provider's live channel on My list is its Live TV channel, a favourite there: apps show favourite channels
    # first in their Live TV channels and guide.
    assert c.post("/api/jf/list", json={"id": news}).json() == {"added": ["News 24"], "already": []}
    assert USER_DATA[news_tv]["IsFavorite"] and not USER_DATA.get(news, {}).get("IsFavorite")
    assert c.post("/api/jf/list", json={"id": news}).json() == {"added": [], "already": ["News 24"]}
    # When My list is read: the list's channels and those favourited in an app as videos move to their Live TV
    # channel; one with no Live TV channel stays on the list; a movie stays a favourite.
    USER_DATA[sports] = {"IsFavorite": True}
    USER_DATA[MOVIE] = {"IsFavorite": True}
    listed = c.get("/api/jf/list").json()
    assert sorted((i["title"], i["type"], i["live"]) for i in listed) == [
        ("ESPN 2 4K", "TvChannel", True),
        ("NEWS 24", "TvChannel", True),
        ("Sports 1", "Video", True),
        ("Wedding Film 4K", "Video", False),
    ]
    assert USER_DATA[espn_tv]["IsFavorite"] and not USER_DATA[sports]["IsFavorite"]
    assert PLAYLISTS["pl1"]["items"] == [sports]
    assert c.delete("/api/jf/list", params={"id": news_tv}).status_code == 204
    assert not USER_DATA[news_tv]["IsFavorite"]
    assert c.delete("/api/jf/list", params={"id": news_tv}).status_code == 404
    # The list's last channel off it: the list goes too (no empty row in Wholphin).
    assert c.delete("/api/jf/list", params={"id": sports}).status_code == 204
    assert PLAYLISTS == {}


def test_my_list_shows_the_last_favourite_first_wherever_it_was_made(tmp_path, monkeypatch):
    _, c, _ = setup(tmp_path)
    clock = iter(range(1_800_000_000, 1_800_001_000, 10))  # 2027: after the library dates
    monkeypatch.setattr("mimir_tv.mylist.time.time", lambda: next(clock))
    # Favourites from before the list noted dates: newest in the library first.
    monkeypatch.setitem(ITEMS[MOVIE], "DateCreated", "2020-01-01T00:00:00.0000000Z")
    monkeypatch.setitem(ITEMS[CLIP], "DateCreated", "2021-01-01T00:00:00Z")
    USER_DATA[MOVIE], USER_DATA[CLIP] = {"IsFavorite": True}, {"IsFavorite": True}
    assert [i["id"] for i in c.get("/api/jf/list").json()] == [CLIP, MOVIE]
    c.delete("/api/jf/list", params={"id": MOVIE})
    c.post("/api/jf/list", json={"id": MOVIE})  # added again here: now the newest
    assert [i["id"] for i in c.get("/api/jf/list").json()] == [MOVIE, CLIP]
    USER_DATA[EP1] = {"IsFavorite": True}  # made a favourite in a Jellyfin app (Wholphin): newest when seen
    listed = c.get("/api/jf/list").json()
    assert [i["id"] for i in listed] == [EP1, MOVIE, CLIP] and listed[0]["added"] > listed[1]["added"]


def test_play_here_keeps_jellyfins_place_and_played_state(tmp_path):
    _, c, _ = setup(tmp_path)
    w = {"id": MOVIE, "session": "5" * 32}  # the movie is an hour long
    assert c.get(f"/api/jf/resume/{MOVIE}").json() == {"start": 0}
    assert c.post("/api/jf/playing", json=w | {"event": "start", "position": 0}).status_code == 204
    c.post("/api/jf/playing", json=w | {"event": "progress", "position": 60})  # under 5%: no place kept yet
    assert USER_DATA[MOVIE]["PlaybackPositionTicks"] == 0
    c.post("/api/jf/playing", json=w | {"event": "progress", "position": 600, "paused": True})
    assert USER_DATA[MOVIE]["PlaybackPositionTicks"] == 6_000_000_000 and USER_DATA[MOVIE]["LastPlayedDate"]
    assert c.get(f"/api/jf/resume/{MOVIE}").json() == {"start": 600}  # Play here, the TV and the phone go on from here
    assert [(e, b["PositionTicks"], b["IsPaused"]) for e, b in REPORTS] == [
        ("", 0, False),
        ("Progress", 600_000_000, False),
        ("Progress", 6_000_000_000, True),
    ]
    c.post("/api/jf/playing", json=w | {"event": "stopped", "position": 3400})  # 94%: watched
    data = USER_DATA[MOVIE]
    assert (data["Played"], data["PlayCount"], data["PlaybackPositionTicks"]) == (True, 1, 0)
    assert REPORTS[-1][0] == "Stopped"
    assert c.post("/api/jf/playing", json=w | {"id": "x", "event": "stopped", "position": 1}).status_code == 422


def test_play_here_goes_on_to_the_next_episode(tmp_path):
    _, c, _ = setup(tmp_path)
    nxt = c.get(f"/api/jf/next/{EP1}").json()
    assert (nxt["id"], nxt["title"], nxt["sub"]) == (EP2, "Second", "A Show · S01E02 · 60 min")
    assert c.get(f"/api/jf/next/{EP2}").json() == {}  # the last one
    assert c.get(f"/api/jf/next/{MOVIE}").json() == {}  # not an episode


def test_search_finds_people_by_name_and_a_persons_page_lists_their_titles(tmp_path):
    _, c, _ = setup(tmp_path)
    # Every word of the search is in the name; people with nothing in the library are left out; most titles first.
    people = c.get("/api/jf/people", params={"q": "prabhas"}).json()
    assert [(x["id"], x["name"], x["count"]) for x in people] == [(PRABHAS, "Prabhas", 2)]
    assert people[0]["thumb"] == f"/jf/Items/{PRABHAS}/Images/Primary?maxHeight=480&quality=85"
    assert [x["name"] for x in c.get("/api/jf/people", params={"q": "Deepika"}).json()] == ["Deepika Padukone"]
    assert c.get("/api/jf/people", params={"q": "prabhas padukone"}).json() == []  # Jellyfin's loose match: not one
    assert c.get("/api/jf/people", params={"q": "x"}).json() == []  # too short to mean a name
    page = c.get(f"/api/jf/person/{PRABHAS}").json()
    assert page["name"] == "Prabhas" and page["thumb"].startswith(f"/jf/Items/{PRABHAS}/")
    assert [i["title"] for i in page["items"]] == ["Kalki 2898 AD", "Salaar"]  # newest first
    assert c.get("/api/jf/person/../Users").status_code == 404


def test_a_movie_page_has_its_details_and_the_librarys_titles_like_it(tmp_path):
    _, c, _ = setup(tmp_path)
    page = c.get("/api/jf/title", params={"id": FILM}).json()
    assert (page["title"], page["year"], page["minutes"], page["rating"], page["score"]) == (
        "Kalki 2898 AD",
        2024,
        60,
        "UA",
        7.5,
    )
    assert page["genres"] == ["Science Fiction", "Action"] and page["directors"] == ["Nag Ashwin"]
    assert page["cast"] == ["Prabhas", "Deepika Padukone"]  # actors only: the producer is not cast
    # Each named person opens their own page (their titles here), as in Wholphin.
    assert page["people"] == [
        {
            "id": PRABHAS,
            "name": "Prabhas",
            "role": "Actor",
            "as": "Bhairava",
            "thumb": f"/jf/Items/{PRABHAS}/Images/Primary?maxHeight=240&quality=85",
        },
        {"id": DEEPIKA, "name": "Deepika Padukone", "role": "Actor", "as": "Sumathi", "thumb": ""},
        {"id": "", "name": "Nag Ashwin", "role": "Director", "as": "", "thumb": ""},
    ]
    assert page["backdrop"] == f"/jf/Items/{FILM}/Images/Backdrop?maxWidth=1280&quality=85"
    assert [m["title"] for m in page["more"]] == ["Salaar"]
    # Jellyfin failing the titles like it still shows the page; an id that is not one is refused before Jellyfin.
    other = c.get("/api/jf/title", params={"id": LIKE_IT}).json()
    assert other["more"] == [] and other["backdrop"] == "" and other["cast"] == ["Prabhas"]
    assert c.get("/api/jf/title", params={"id": "../Users"}).status_code == 422
    assert c.get("/api/jf/title", params={"id": "6" * 32}).status_code in (404, 502)


def test_whats_playing_on_the_tv_includes_wholphin_which_takes_no_remote_control_while_it_plays(tmp_path, monkeypatch):
    playing = {
        "Id": "w",
        "UserId": "u1",
        "Client": "Wholphin",
        "SupportsRemoteControl": False,
        "NowPlayingItem": {
            "Id": "e3",
            "Name": "Episode 3",
            "SeriesName": "Dark",
            "IndexNumber": 3,
            "ParentIndexNumber": 1,
        },
        "PlayState": {"IsPaused": False, "PositionTicks": 12 * 600_000_000},
    }
    someone_else = playing | {"Id": "x", "UserId": "u2", "NowPlayingItem": {"Id": "z", "Name": "Not ours"}}
    monkeypatch.setattr("tests.test_jf.SESSIONS", [someone_else, playing])
    _, c, _ = setup(tmp_path)
    now = c.get("/api/jf/now").json()
    assert (now["show"], now["episode"], now["minute"]) == ("Dark", "S01E03", 12)
    monkeypatch.setattr("tests.test_jf.SESSIONS", [playing | {"NowPlayingItem": None}])
    assert c.get("/api/jf/now").json() == {}


def test_search_finds_whats_on_tv_now_and_later_today_from_the_guide(tmp_path, monkeypatch):
    def at(minutes):
        return time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(time.time() + minutes * 60))

    def show(name, start, end, channel="ch1", channel_name="Sky Sports Football"):
        return {
            "Id": f"p-{name}-{start}",
            "Name": name,
            "Type": "Program",
            "StartDate": at(start),
            "EndDate": at(end),
            "ChannelId": channel,
            "ChannelName": channel_name,
            "ChannelPrimaryImageTag": "c",
        }

    monkeypatch.setattr(
        "tests.test_jf.PROGRAMMES",
        [
            show("Premier League: Arsenal v Chelsea", -30, 75),  # on now
            show("Arsenal Highlights", 180, 210, "ch2", "Arsenal TV"),  # later today
            show("Arsenal Classics", -300, -240),  # over: left out
            show("Arsenal Weekly", 60 * 30, 60 * 31),  # past tomorrow: left out
            show("Chelsea: The Story", 30, 60),  # Jellyfin's loose match on "arsenal chelsea": not both words
        ],
    )
    _, c, _ = setup(tmp_path)
    found = c.get("/api/jf/programmes", params={"q": "arsenal"}).json()
    assert [(x["title"], x["channel"], x["on_now"]) for x in found] == [
        ("Premier League: Arsenal v Chelsea", "Sky Sports Football", True),
        ("Arsenal Highlights", "Arsenal TV", False),
    ]
    # Each plays its channel: the card is the channel's (its id, its logo), with the programme's name and times.
    first = found[0]
    assert (first["id"], first["type"], first["kind"]) == ("ch1", "TvChannel", "video")
    assert first["thumb"] == "/jf/Items/ch1/Images/Primary?maxHeight=480&quality=85"
    assert first["start"] < first["end"]
    assert [x["title"] for x in c.get("/api/jf/programmes", params={"q": "arsenal chelsea"}).json()] == [
        "Premier League: Arsenal v Chelsea"
    ]
    assert c.get("/api/jf/programmes", params={"q": "x"}).json() == []


def test_a_short_search_word_starts_a_word_or_is_initials_never_inside_a_longer_one(tmp_path, monkeypatch):
    names = {
        "1a" * 16: ("NBA Tonight", "Video"),
        "1b" * 16: ("Nanbagal Seythigal", "Series"),  # "nba" inside a word: not a match
        "1c" * 16: ("M.S. Dhoni- The Untold Story (2016)", "Movie"),  # "ms": its initials
        "1d" * 16: ("Thomas & Friends", "Series"),  # "ms" inside a word: not a match
        "1e" * 16: ("Dhoom 2", "Movie"),
    }
    for id_, (name, kind) in names.items():
        monkeypatch.setitem(ITEMS, id_, item(id_, name, kind))
    _, c, _ = setup(tmp_path)

    def search(q):
        return sorted(i["title"] for i in c.get("/api/jf/search", params={"q": q}).json())

    assert search("nba") == ["NBA Tonight"]
    assert search("ms dhoni") == ["M.S. Dhoni- The Untold Story (2016)"]
    assert search("dho") == ["Dhoom 2", "M.S. Dhoni- The Untold Story (2016)"]  # the start of a word, while typing
    assert search("hom") == []  # not the middle of "Thomas"
    assert search("homas") == ["Thomas & Friends"]  # a longer word may be anywhere in a name, as before


def test_search_is_wholphins_a_group_per_kind_closest_names_first_and_spelling_slips_forgiven(tmp_path, monkeypatch):
    names = {
        "2a" * 16: ("The Dark Knight", "Movie"),
        "2b" * 16: ("Dark Waters", "Movie"),
        "2c" * 16: ("Dark", "Series"),
        "2d" * 16: ("Darkwing Duck", "Series"),
        "2e" * 16: ("Into the Dark", "Series"),
        "2f" * 16: ("Dark Shows", "BoxSet"),  # a collection
        "3a" * 16: ("Telugu News", "TvChannel"),
        "3b" * 16: ("Dark Folder", "Folder"),  # a live channel's own folder (its .strm is the Video): not a row
    }
    for id_, (name, kind) in names.items():
        monkeypatch.setitem(ITEMS, id_, item(id_, name, kind))
    _, c, _ = setup(tmp_path)

    def search(q):
        return [(i["type"], i["title"]) for i in c.get("/api/jf/search", params={"q": q}).json()]

    # Movies, then shows, then the other kinds (Wholphin's rows), each closest first: the exact name, then a name
    # starting with it, then a word starting with it, then anywhere in a word.
    assert search("dark") == [
        ("Movie", "Dark Waters"),
        ("Movie", "The Dark Knight"),
        ("Series", "Dark"),
        ("Series", "Darkwing Duck"),
        ("Series", "Into the Dark"),
        ("BoxSet", "Dark Shows"),
    ]
    assert search("telgu news") == [("TvChannel", "Telugu News")]  # a letter left out, as Jellyfin forgives
    # One Jellyfin search a query, sorted into rows here: a search a kind (eight at once) piled up in a slow search
    # index until Jellyfin stopped answering anything.
    SEARCHED.clear()
    search("dark")
    assert SEARCHED == ["dark"]
    assert search("drak") == []  # a short word is not guessed at


def test_cards_carry_what_wholphins_rows_show_and_watched_can_be_set_either_way(tmp_path, monkeypatch):
    # A wide picture for the landscape rows (Continue watching): a movie's backdrop, an episode's or a video's own
    # still; its length, so the page can say how much is left; whether it was watched.
    monkeypatch.setitem(ITEMS[FILM], "UserData", {"Played": True})
    _, c, _ = setup(tmp_path)
    film = c.get("/api/jf/title", params={"id": FILM}).json()
    assert (film["wide"], film["minutes"], film["played"]) == (
        f"/jf/Items/{FILM}/Images/Backdrop?maxWidth=780&quality=85",
        60,
        True,
    )
    pilot = c.get("/api/jf/title", params={"id": EP1}).json()
    assert (pilot["wide"], pilot["played"]) == (f"/jf/Items/{EP1}/Images/Primary?maxWidth=780&quality=85", False)
    assert c.get("/api/jf/title", params={"id": LIKE_IT}).json()["wide"] == ""  # a poster only: no wide picture

    assert c.post("/api/jf/watched", json={"id": EP1, "on": True}).json() == {"played": True}
    assert USER_DATA[EP1]["Played"]
    assert c.post("/api/jf/watched", json={"id": EP1, "on": False}).json() == {"played": False}
    assert not USER_DATA[EP1]["Played"]
    assert c.post("/api/jf/watched", json={"id": "../Users", "on": True}).status_code == 422


def test_a_shows_page_has_its_details_seasons_next_episode_and_episodes_with_their_watch_state(tmp_path, monkeypatch):
    series = item(SHOW, "A Show", "Series", ProductionYear=2022, OfficialRating="TV-14", CommunityRating=8.04)
    monkeypatch.setitem(ITEMS, SHOW, series | {"Overview": "Two episodes.", "BackdropImageTags": ["b"]})
    monkeypatch.setattr("tests.test_jf.NEXT_UP", [ITEMS[EP2]])
    _, c, _ = setup(tmp_path)
    USER_DATA[EP1] = {"Played": True}
    USER_DATA[EP2] = {"PlaybackPositionTicks": 9_000_000_000}  # a quarter in
    show = c.get(f"/api/jf/show/{SHOW}").json()
    assert (show["title"], show["year"], show["rating"], show["score"], show["desc"]) == (
        "A Show",
        2022,
        "TV-14",
        8.0,
        "Two episodes.",
    )
    assert show["backdrop"] == f"/jf/Items/{SHOW}/Images/Backdrop?maxWidth=1280&quality=85"
    assert [(x["name"], x["n"]) for x in show["seasons"]] == [("Season 1", 1)]
    assert (show["next"]["id"], show["next"]["title"]) == (EP2, "Second")  # the main button plays it
    assert [(e["season"], e["n"], e["title"], e["played"], e["progress"]) for e in show["episodes"]] == [
        (1, 1, "Pilot", True, 0),
        (1, 2, "Second", False, 25),
    ]
    assert c.get("/api/jf/show/../Users").status_code == 404


def test_live_tv_lists_whats_on_now_by_kind_each_card_playing_its_channel(tmp_path, monkeypatch):
    def at(minutes):
        return time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(time.time() + minutes * 60))

    def show(name, channel, **kind):
        return {
            "Id": f"p-{name}",
            "Name": name,
            "Type": "Program",
            "StartDate": at(-20),
            "EndDate": at(40),
            "ChannelId": channel,
            "ChannelName": channel.upper(),
            "ChannelPrimaryImageTag": "c",
        } | kind

    monkeypatch.setattr(
        "tests.test_jf.PROGRAMMES",
        [
            show("Cricket: Final", "ch1", IsSports=True),
            show("Evening Bulletin", "ch2", IsNews=True),
            show("Cartoons", "ch3", IsKids=True),
            show(
                "Schedule not available", "ch4", IsNews=True
            ),  # a guide's filler for a slot it does not know: left out
        ],
    )
    _, c, _ = setup(tmp_path)
    live = c.get("/api/jf/live").json()
    assert [r["name"] for r in live["rows"]] == ["On now", "Sports", "News", "Kids"]  # a kind with nothing on: no row
    assert [x["title"] for x in live["rows"][2]["items"]] == ["Evening Bulletin"]
    sports = live["rows"][1]["items"]
    assert [(x["title"], x["channel"], x["id"], x["type"], x["on_now"]) for x in sports] == [
        ("Cricket: Final", "CH1", "ch1", "TvChannel", True)
    ]
    assert 30 <= sports[0]["progress"] <= 36  # 20 minutes into an hour


def test_live_tvs_channels_come_in_the_tuners_groups_favourites_first_with_their_guide(tmp_path, monkeypatch):
    aaj, star = "a1" * 16, "a2" * 16
    monkeypatch.setitem(ITEMS, "9" * 32, ITEMS["9" * 32] | {"ChannelNumber": "1"})
    monkeypatch.setitem(ITEMS, aaj, item(aaj, "AAJ TAK", "TvChannel", ChannelNumber="2"))
    monkeypatch.setitem(ITEMS, star, item(star, "STAR SPORTS 1", "TvChannel", ChannelNumber="3"))
    monkeypatch.setattr(
        "tests.test_jf.TUNER_M3U",
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="1" tvg-name="NDTV 24x7" group-title="English · News",NDTV 24x7\nhttp://x/1.ts\n'
        '#EXTINF:-1 tvg-id="2" tvg-name="AAJ TAK" group-title="Hindi · News",AAJ TAK\nhttp://x/2.ts\n'
        '#EXTINF:-1 tvg-id="3" tvg-name="STAR SPORTS 1" group-title="Sports · Cricket",STAR SPORTS 1\nhttp://x/3.ts\n',
    )

    def at(minutes):
        return time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime(time.time() + minutes * 60))

    def show(name, start, end):
        return {"Id": f"p-{name}", "Name": name, "StartDate": at(start), "EndDate": at(end), "ChannelId": aaj}

    monkeypatch.setattr(
        "tests.test_jf.PROGRAMMES", [show("Bulletin", -20, 40), show("Late News", 40, 100), show("Tomorrow", 600, 660)]
    )
    _, c, _ = setup(tmp_path)
    USER_DATA[star] = {"IsFavorite": True}

    live = c.get("/api/jf/channels").json()
    assert live["groups"] == [
        {"name": "English · News", "count": 1},
        {"name": "Hindi · News", "count": 1},
        {"name": "Sports · Cricket", "count": 1},
    ]
    # Favourites first (as Streamyfin and Wholphin list them), then by number; each with its group and guide.
    assert [(x["name"], x["fav"], x["group"]) for x in live["channels"]] == [
        ("STAR SPORTS 1", True, "Sports · Cricket"),
        ("NDTV 24x7", False, "English · News"),
        ("AAJ TAK", False, "Hindi · News"),
    ]
    aaj_tak = live["channels"][2]
    assert [(p["title"], p["on_now"]) for p in aaj_tak["programmes"]] == [("Bulletin", True), ("Late News", False)]
    assert 30 <= aaj_tak["programmes"][0]["progress"] <= 36  # the guide's next three hours; "Tomorrow" is past them
    assert aaj_tak["thumb"].startswith(f"/jf/Items/{aaj}/Images/Primary")
    assert live["total"] == 3

    # A group, the favourites, a page at a time.
    assert [x["name"] for x in c.get("/api/jf/channels", params={"group": "Hindi · News"}).json()["channels"]] == [
        "AAJ TAK"
    ]
    assert [x["name"] for x in c.get("/api/jf/channels", params={"fav": "true"}).json()["channels"]] == [
        "STAR SPORTS 1"
    ]
    page = c.get("/api/jf/channels", params={"offset": 1, "limit": 1}).json()
    assert ([x["name"] for x in page["channels"]], page["total"]) == (["NDTV 24x7"], 3)
