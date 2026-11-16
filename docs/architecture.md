# mimir-tv architecture

mimir-tv fills Jellyfin and finds what is missing from it. On the Mac, its jobs write a provider's catalogue and
the streaming apps' popular titles into Jellyfin, and lay out Wholphin's home rows and collections, so any Jellyfin
app shows them. On the phone, its page searches every source and adds a missing title. It plays nothing itself:
Jellyfin plays your videos and anything added from a provider (Wholphin on the TV, Streamyfin or Swiftfin on the
phone), and a streaming title opens in its app.
GitHub draws the diagrams below (Mermaid). When the architecture changes, update this file in the same commit.

## What runs where

```mermaid
flowchart LR
  subgraph phone["Phone / Mac browser"]
    pwa["mimir-tv page<br/>(Home Screen app)"]
    swift["Streamyfin / Swiftfin / Jellyfin web"]
    apps_p["Streaming apps<br/>(their web links)"]
  end
  subgraph tv["Android TV or Fire TV (no Tailscale)"]
    jftv["Wholphin<br/>(Jellyfin app)"]
    tvapp["mimir-tv TV app<br/>(doodles and photos; ?tv=1)"]
    kodi["Kodi (optional)"]
    apps_tv["Netflix, Peacock, Prime Video …"]
  end
  subgraph mac["The home Mac"]
    caddy["Caddy<br/>tv.home.&lt;domain&gt;<br/>lan.home.&lt;domain&gt;"]
    app["mimir-tv app<br/>127.0.0.1:8723"]
    res["Resolver<br/>LAN listener :8724<br/>/play /hls /jf"]
    jf["Jellyfin :8096"]
    db[("mimir-tv.db<br/>providers/&lt;id&gt;/")]
    strm["media_root/&lt;id&gt;*<br/>.strm files"]
  end
  prov["A provider<br/>(plugin, optional)"]
  tmdb["TMDB<br/>(where titles stream)"]

  pwa -- "https, tailnet" --> caddy
  tvapp -- "https, LAN" --> caddy
  caddy --> app
  caddy -- "/play /hls /jf" --> res
  app --> db
  app -- "API key" --> jf
  app -- "writes" --> strm
  jf -- "reads" --> strm
  jftv -- "browse, play" --> jf
  swift -- "browse, play" --> jf
  jftv -- ".strm link" --> res
  res -- "its login" --> prov
  app -- "its login" --> prov
  app -- "token in a header" --> tmdb
  app -- "ADB: open an app" --> tv
  app -- "JSON-RPC" --> kodi
  pwa -.-> swift
  pwa -.-> apps_p
```

- The phone reaches the Mac over Tailscale; the TV has no Tailscale, so `lan.home` resolves to the Mac's LAN address
  and serves the resolver (and the optional TV app's page).
- Wholphin and the phone's Jellyfin apps talk to Jellyfin only. What mimir-tv adds reaches them as Jellyfin items,
  tags, collections, posters and display preferences, so they need nothing from mimir-tv at play time except the
  resolver for a provider's links.
- Providers are optional. A plugin provider (catalog.py) is in Settings' `providers` with a kind an installed plugin
  registers; without the plugin it is left out (`GET /api/connectors` lists the ones the page shows). Without a TMDB
  token, search leaves out the streaming apps.
- Secrets stay on the Mac: providers' logins and Kodi's password in the vault (mimir-tv.db, encrypted, the key in
  the Keychain); the Jellyfin API key, TMDB token and ADB key in owner-only files. The browser never talks to
  Jellyfin or a provider directly.

## Main paths

### Search

```mermaid
sequenceDiagram
  participant P as Page
  participant A as mimir-tv
  participant J as Jellyfin
  participant S as A provider
  participant T as TMDB
  P->>A: GET /api/jf/search?q
  A->>J: search (API key)
  P->>A: GET /api/p/<id>/search?q (each provider)
  A->>S: the plugin's own search
  P->>A: GET /api/streaming/search?q (token set)
  A->>T: search/multi, then watch/providers per title (cached a day)
  A-->>P: titles on the house's apps (streaming_apps)
  Note over P: A streaming title also in Jellyfin or a provider shows once, with its apps
```

### Add a provider's show to Jellyfin and play it on the TV

```mermaid
sequenceDiagram
  participant P as Page
  participant A as mimir-tv
  participant J as Jellyfin
  participant TV as TV
  participant R as Resolver
  participant S as The provider
  P->>A: POST /api/p/<id>/jellyfin (show, tv=true)
  A->>A: write Show/Season NN/*.strm (each points at the resolver)
  A->>J: scan the library, find the episode
  A->>TV: ADB: wake, open the Jellyfin TV app (if closed)
  A->>J: Sessions: play the episode on the TV's client
  TV->>R: GET /play?p=<id>&path=episode link
  R->>S: the plugin's link() (signed with its login, where it signs links)
  R-->>TV: 302 to the signed link
  Note over A: Hourly: new episodes of added shows get .strm files and a scan
```

### Live TV

A provider's live channel may be a plain-http HLS stream, which https pages refuse (the plugin says so: relayed()).
The resolver's `/hls` relays the playlist, rewriting every variant, segment and key to come back through it, and
only for stream servers a provider handed out (not an open proxy). A link that expires is replaced from its list:
the .strm line carries the list (`page=`) and the plugin's link() finds the fresh one there.

### Into Jellyfin

```mermaid
flowchart LR
  set["Settings: Into Jellyfin<br/>(languages, numbers)"] --> mirror["mirror.py<br/>whole catalogue, hourly"]
  set --> onapps["onapps.py<br/>On my apps, daily"]
  mirror -- ".strm + .nfo tags" --> jf["Jellyfin libraries"]
  onapps -- ".strm to /api/streaming/launch" --> jf
  badges["badges.py"] -- "poster.jpg" --> jf
  grouping["grouping.py"] -- "collections per language and kind" --> jf
  wholphin["wholphin.py"] -- "home rows (display preferences)" --> jf
  jf --> w["Wholphin on each TV"]
```

One ordered list of languages (`library_languages`) drives every step. A title the house watched, favourited or
added by hand is never sent to the Trash. A movie in another language is its own title ("Title · Language"), and a
hand-added one keeps its folder when the mirror runs.

### A title on a streaming app

```mermaid
sequenceDiagram
  participant P as Page
  participant A as mimir-tv
  participant TV as TV
  alt phone
    P->>P: the app's web link (the app opens)
  else TV, or 📺 from the phone
    P->>A: POST /api/streaming/open {app, title}
    A->>TV: ADB: wake; am start SEARCH in the app's package (title shell-quoted), else open the app
  end
```

### Jobs

Each run is recorded with jobkit in `mimir-tv.db` (tables `jobkit_jobs`, `jobkit_runs`); `/jobs` shows them,
with Pause and Run now. A job that fails twice in a row sends one notice (jobkit's notify-url: `openclaw:<phone>`
pushes the title to the OpenClaw iPhone app; any other URL gets an ntfy-style POST).

| Job | Schedule | Does |
|---|---|---|
| `backup` | nightly 3:30 (when `backup_dir` is set) | mimir-tv's and Jellyfin's settings, lists and watch history; no keys or passwords |
| `offsite` | after each backup | The backup folder to the rclone remote in settings (encrypted) |
| `new-episodes` | hourly | New episodes of added shows into Jellyfin |
| `whole-catalogue` | hourly | A provider's whole catalogue into Jellyfin (`mirror.py`), a batch of shows each run |
| `on-my-apps` | daily | The streaming apps' popular titles into Jellyfin (`onapps.py`) |
| `<id>-<job>` | the plugin's (checked hourly) | A provider's own jobs (catalog.Job), e.g. an index for search |

## Code map (`service/mimir_tv/`)

| Module | Owns |
|---|---|
| [app.py](../service/mimir_tv/app.py) | Startup (`main`): settings, connectors, routers, schedules; the Library page API; `/`, `/api/version`, `/api/connectors` |
| [settings.py](../service/mimir_tv/settings.py) | `settings.json` defaults, environment overrides |
| [wizard.py](../service/mimir_tv/wizard.py), [setup.py](../service/mimir_tv/setup.py), [checks.py](../service/mimir_tv/checks.py) | The Setup steps and the Settings page (one save path), Jellyfin's first run and key, live ✓/✗ checks |
| [jellyfin.py](../service/mimir_tv/jellyfin.py) | Jellyfin's API (libraries, folders, scans, search, sessions) |
| [jf_routes.py](../service/mimir_tv/jf_routes.py) | Your videos: browse, search, and the `/jf/...` proxy that adds the API key |
| [catalog.py](../service/mimir_tv/catalog.py) | The provider plugin interface (Catalog, Job), finding installed plugins, the providers in Settings and their catalogues |
| [catalog_routes.py](../service/mimir_tv/catalog_routes.py) | A provider's pages (`/api/p/<id>/…`): home rows, browse, search, live, play, add to Jellyfin; thumbnails and the relay |
| [providers.py](../service/mimir_tv/providers.py) | Settings' providers: IPTV (M3U, Xtream) through Jellyfin's Live TV, and adding a plugin provider |
| [strm.py](../service/mimir_tv/strm.py) | `.strm` files for shows, movies and live channels; new episodes |
| [resolver.py](../service/mimir_tv/resolver.py), [hls.py](../service/mimir_tv/hls.py) | The LAN listener players fetch from; the HLS relay |
| [into.py](../service/mimir_tv/into.py) | Settings → Into Jellyfin: the languages and numbers, the preview, Live TV's channel groups |
| [mirror.py](../service/mimir_tv/mirror.py) | The whole catalogue in Jellyfin: movies and shows as `.strm` with `.nfo` tags; dubs as their own titles; the Trash for titles that left |
| [onapps.py](../service/mimir_tv/onapps.py) | On my apps: the streaming apps' popular titles as Jellyfin items that open the app |
| [badges.py](../service/mimir_tv/badges.py) | Language and app badges drawn on posters, so every Jellyfin app shows them |
| [grouping.py](../service/mimir_tv/grouping.py) | One Jellyfin collection per language, holding one per kind |
| [wholphin.py](../service/mimir_tv/wholphin.py) | Wholphin's home rows, written to the user's Jellyfin display preferences |
| [tvsetup.py](../service/mimir_tv/tvsetup.py) | `mimir-tv-setup <tv>`: another TV's apps, screensaver and Wholphin sign-in |
| [tvmessage.py](../service/mimir_tv/tvmessage.py) | A doodle or photo from the phone shown over the TV for a few seconds (the TV app's overlay) |
| [taste.py](../service/mimir_tv/taste.py) | What was watched, 👍/👎 and the house's rules, for an assistant that recommends |
| [backup.py](../service/mimir_tv/backup.py) | The nightly backup and its off-site copy |
| [vault.py](../service/mimir_tv/vault.py) | Logins encrypted in the database, the key in the Keychain |
| [mylist.py](../service/mimir_tv/mylist.py) | When each Jellyfin favourite went on My list (newest first) |
| [streaming.py](../service/mimir_tv/streaming.py) | Where titles stream (TMDB), the app catalogue and filter, open on the TV |
| [tv.py](../service/mimir_tv/tv.py), [kodi.py](../service/mimir_tv/kodi.py) | The TV over ADB (open Jellyfin or an app, what is on screen); Kodi's JSON-RPC |
| [recs.py](../service/mimir_tv/recs.py) | Recommendations (E2): each category is a TMDB discover query; the person's filters (apps, language, genre, kind, rating, years) go to TMDB; watched titles left out; a one-line why from the data |
| [prereqs.py](../service/mimir_tv/prereqs.py) | The Setup page (`/setup`): what this Mac needs (Homebrew, uv, Jellyfin, Tailscale), checked; installs one part only on a confirmed request from the Mac itself |
| [mcp_server.py](../service/mimir_tv/mcp_server.py) | `POST /mcp` for an assistant (OpenClaw): tools that call the app's own routes in-process; bearer token, tailnet only |
| [jobs.py](../service/mimir_tv/jobs.py) | The scheduled jobs and the jobs page routes |
| [static/index.html](../service/mimir_tv/static/index.html) | The whole page: phone, Mac and TV mode (`?tv=1`, remote navigation) |
| [android/](../android/) | The TV app (the doodle and photo overlay): a WebView on mimir-tv's `?tv=1`, the address typed on first launch; mimir-tv serves its APK at `/tv/app.apk` |
