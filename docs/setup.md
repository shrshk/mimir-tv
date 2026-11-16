# Set up mimir-tv from scratch

One Mac at home runs Jellyfin and mimir-tv. Phones reach it over Tailscale; TVs on the home Wi-Fi. A domain of your
own is optional: everything below works without one, and [the last part](#optional-your-own-domain) says what a
domain adds.

Time: about an hour of your own, then a day or so while the jobs fill Jellyfin.

## What you end up with

| Where | App | For |
|---|---|---|
| The home Mac | Jellyfin, mimir-tv | The library, the jobs that fill it, the resolver that plays a provider's links |
| Phones | mimir-tv (Home Screen app), Streamyfin | Find a title and add it to Jellyfin or My list; watch on the phone |
| TVs | Wholphin | Watch: home rows by app and language, collections, Live TV, My list |

You choose what goes in. **Streams only** (no videos of your own) skips "Your media" and needs a provider; see
[Streams only](#streams-only).

## Before you start

- **A Mac that stays on**, at home, on the home Wi-Fi (Apple Silicon works best: Jellyfin uses its hardware video).
  System Settings → Energy (or Battery → Options on a laptop): prevent automatic sleeping, and start up after a
  power failure where offered.
- **Disk**: a few GB for Jellyfin's data. A provider's catalogue is `.strm` files, a few KB each; your own videos
  take what they take.
- **[Homebrew](https://brew.sh)** on the Mac.
- **A Tailscale account** (the free plan is enough). One account is one tailnet: every device signed in to it can
  reach the Mac; nothing else can.
- Optional:
  - **A provider plugin** and its login, for a provider's catalogue (see [the README](../README.md#provider-plugins)).
    Each house uses its own login.
  - **A TMDB account** (free), for "which of my streaming apps has it" and On my apps.
  - **An Android TV or Fire TV** for Wholphin.

## 1. Tailscale

1. Sign up at [tailscale.com](https://tailscale.com) (a new account makes a new tailnet).
2. In the [admin console](https://login.tailscale.com/admin/dns): **DNS** → turn on **MagicDNS** and **HTTPS
   Certificates**. mimir-tv's phone link is an https address that Tailscale certifies.
3. Install Tailscale on the Mac (App Store or tailscale.com/download) and sign in.
4. Install Tailscale on each phone and sign in with the same account.

The TVs do not need Tailscale: they use the home Wi-Fi.

## 2. Install

On the Mac, in Terminal:

```sh
git clone https://github.com/shrshk/mimir-tv && cd mimir-tv
scripts/setup-mac.sh
```

It asks before installing anything: uv (runs mimir-tv) and Jellyfin, both with Homebrew. Then it starts Jellyfin and
mimir-tv at login and opens the Setup page, `http://127.0.0.1:8723/setup`. Running it again is safe.

The top of the Setup page lists what this Mac has (Homebrew, uv, Jellyfin, Tailscale), with an Install button for
what is missing. Install works only on the Mac itself, and asks first.

## 3. The Setup page

One step at a time. Only Jellyfin is required; skip any other step and do it later on the Settings page.

| Step | What to enter |
|---|---|
| **Jellyfin** | A user name and password. On a new Jellyfin this user becomes its admin; mimir-tv makes its own API key from it and does not keep the password. |
| **Your media** | Your own videos: pick folders for Movies, Shows and Home videos on the Library page. **Streams only: skip.** |
| **Your streaming apps** | Optional. A TMDB token (themoviedb.org → Settings → API → *API Read Access Token*, the long one), your apps (`netflix, prime, peacock` …) and country. |
| **Your languages** | The languages the house watches, in order (`Telugu, Hindi, English`). They decide what a provider and On my apps put in Jellyfin, and Wholphin's rows. |
| **The TV** | Optional. The TV's IP address (TV: Settings → Network) and kind (Android TV or Fire TV). Pairing comes in [step 7](#7-tvs). |
| **Phones** | Keep the defaults (below). |
| **Backups** | A folder for the nightly backup (3:30): mimir-tv's and Jellyfin's settings, lists and watch history, not the videos. A drive is best. |

**Phones step, without a domain:**

- **Resolver address**: leave it as offered, `http://<this Mac's home address>:8724`. Jellyfin's apps open a
  provider's links there, so they play on the home Wi-Fi (away from home, see [below](#away-from-home)).
- **Phones open mimir-tv through Tailscale**: on. The link is `https://<this Mac's Tailscale name>:8726`.
- **mimir-tv listens on the home network too, for the TV**: on, if you want the mimir-tv TV app (doodles and photos on
  the TV). Off otherwise.

Press **Finish**. Setup locks; from now on the **Settings** page (⚙ in mimir-tv's header, or `/settings`) shows what
works with ✓, what does not with ✗ and the fix, and changes anything. Settings changes only from the Mac itself or a
tailnet device.

## 4. A provider

Skip this without a provider plugin.

1. Put the plugin on the Mac, then list it in `~/.config/mimir-tv/plugins`, one line: the plugin's folder, or a
   package spec uv accepts (`git+https://…`).
2. Run `scripts/install-mac.sh` (it installs mimir-tv with its plugins and restarts it).
3. Settings → **Providers**: pick the plugin's kind, give it a name (the name of its Jellyfin libraries:
   `<name>`, `<name> Movies`, `<name> Live`), and its login. mimir-tv checks the login before it keeps it, in its
   encrypted vault (the key in this Mac's Keychain).

## 5. Fill Jellyfin

Settings → **Into Jellyfin**:

1. **Languages**: as in Setup, in order.
2. **Whole catalogue**: on for the provider. **Newest titles of each category**: start with 50 (0 is all of it: much
   bigger, and many hourly runs to catch up). **Latest episodes of each show**: 10 (a show you add by hand keeps every
   episode).
3. **On my apps** (with a TMDB token): the popular titles on your streaming apps, **Titles per app** of each; each
   opens its app when played.
4. **Wholphin**: tick "Lay out its home".
5. **Preview** says what this would put in Jellyfin and what would go to the Trash. Then **Save**.

Then `/jobs` → **Whole catalogue** → **Run now** (and **On my apps**). The whole catalogue adds the movies at once and
the shows a batch each hour; its line on `/jobs` says how many shows are still waiting. Search in Jellyfin's apps
finds new titles a few minutes after each run.

## 6. Phones

1. Open the phone link from Settings → **Phones and the TV** (or scan its QR code) in Safari, with Tailscale on.
2. Share → **Add to Home Screen**: mimir-tv opens like an app.
3. Install **Streamyfin** (or Swiftfin) and add the server `http://<this Mac's Tailscale name>:8096`. Plain http is
   fine here: Tailscale encrypts the connection. Sign in as the Jellyfin user.
4. Settings → **Phones and the TV** → the iPhone's Jellyfin app: Streamyfin (or Swiftfin), so ▶ Jellyfin opens
   the right one.

With more people: give each a Jellyfin user of their own in Jellyfin's dashboard, so each has their own My list and
watch history. mimir-tv itself uses the admin user.

## 7. TVs

**Wholphin** (the Jellyfin app the house uses on the TV):

1. Install Wholphin: Google Play on an Android TV, the Amazon Appstore on a Fire TV.
2. Open it, pick the server (`http://<this Mac's home address>:8096`) and **Add user**: it shows a Quick Connect
   code. Approve it on the phone (Jellyfin's web page, signed in as that user: your user menu → **Quick Connect**),
   or with `mimir-tv-setup <tv> --approve CODE` once the TV is paired.
3. Wholphin: Settings → Home → use the server's layout. mimir-tv writes the rows: Continue watching, Live channels,
   your apps, then each language.

**Pairing** (optional) lets the Mac wake the TV, open apps and see what plays: 📺 TV on the phone, On my apps titles
opening their app, "what's on TV" for an assistant, and `mimir-tv-setup`. Watching in Wholphin does not need it.

1. Turn on the TV's debugging:
   - Android TV / Google TV: Settings → System → About → press **Build** seven times; then Developer options →
     **USB debugging** (and **Network debugging** where it is a separate switch).
   - Fire TV: Settings → My Fire TV → Developer Options → **ADB debugging** (no Developer Options? My Fire TV →
     About → press the stick's name seven times).
2. Settings → TVs → **Pair**, then on the TV tick **Always allow from this computer** and OK.
3. Optional: `mimir-tv-setup <tv>` checks the TV's apps, opens the store page of a missing one, and sets Immich TV as
   the screensaver.

Accept only a prompt you started: pairing gives this Mac a shell on the TV. Fire TV Sticks that run Vega OS (not
Android) cannot run Wholphin or pair.

**The mimir-tv TV app** (optional) shows doodles and photos from the phone (✏️ TV) over whatever is on the TV: on the
TV, install the Downloader app and open `http://<this Mac's home address>:8723/tv/app.apk`; Settings → TV app has the
steps.

## Away from home

Without a domain:

| | At home | Away (Tailscale on) |
|---|---|---|
| mimir-tv on the phone | ✓ | ✓ |
| Your own videos in Streamyfin | ✓ | ✓ |
| A provider's titles in Streamyfin or Wholphin | ✓ | ✗: their links point at the Mac's home address |
| A provider's titles in mimir-tv's Play here | ✓ | ✓ |

To make the third row work away from home too, let the tailnet reach the home network (a **subnet route**):

1. On the Mac: `tailscale set --advertise-routes=192.168.1.0/24` (your home network's range; the Mac's address
   tells you: `192.168.1.x` → `192.168.1.0/24`). With the App Store Tailscale, the command is
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.
2. Admin console → Machines → the Mac → **Edit route settings** → approve the route.
3. Phones: Tailscale accepts the route on its own (on a Mac or PC client, turn on "Use Tailscale subnets").

## Optional: your own domain

A domain gives every part an https name (`tv.home.<domain>`, `jellyfin.home.<domain>`, `lan.home.<domain>` for the
resolver), so Safari, Swiftfin's native player and Jellyfin's web app on https play a provider's links, at home and
away, with no subnet route. It needs a domain at a DNS provider with an API (Cloudflare works), Caddy on the Mac with
a wildcard certificate, and DNS names pointing at the Mac's Tailscale and home addresses. The steps are in the
README's [Your own domain](../README.md#your-own-domain-optional-advanced). Never use a public tunnel or Tailscale
Funnel for it: mimir-tv is not built to face the internet.

## Streams only

For a house that watches a provider's catalogue and nothing of its own:

- Setup: **skip** Your media (and the TV, if pairing is not wanted).
- [Step 4](#4-a-provider) and [step 5](#5-fill-jellyfin): the provider and the whole catalogue.
- Streaming apps (TMDB): optional. Without it, search covers Jellyfin and the provider only, and On my apps is off.
- Backups still matter: they keep My list and watch history.

## Keep it running

- **Updates**: `cd mimir-tv && git pull && scripts/install-mac.sh`. It restarts mimir-tv: do it when nothing plays.
- **Jobs**: `/jobs` shows each scheduled job (backup, new episodes, whole catalogue, On my apps, the provider's own)
  with its last run, Pause and Run now. A job that fails twice in a row can send a notice (see the README).
- **Restore** on a new Mac: [step 2](#2-install), then `scripts/restore-mac.sh <backup>`, then sign in to Jellyfin
  and save the provider's login again (backups hold no keys or passwords).
- **Remove**: `scripts/install-mac.sh --remove` (Jellyfin, your files and settings stay).

## When something does not work

Settings shows ✓ or ✗ for each part, with the fix. The common ones:

| You see | Fix |
|---|---|
| The phone link does not open | Tailscale on the phone is off, or HTTPS Certificates are off in the admin console |
| A provider's title does not play in Streamyfin away from home | Expected without a domain: use a [subnet route](#away-from-home) or mimir-tv's Play here |
| Search in Wholphin does not find a new title | Wait a few minutes after the job's run; titles added by hand are found at once |
| 📺 TV says the TV does not trust this Mac | Pair the TV again and accept the prompt on the TV |
| A provider shows ✗ in Settings | Its login changed, or its plugin is not installed: check `~/.config/mimir-tv/plugins` and run `scripts/install-mac.sh` |
