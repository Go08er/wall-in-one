# Searching and downloading wallpapers

Wall-in-One can pull wallpapers from two sites: **Wallhaven** for stills and
**MotionBGS** for video wallpapers. The search button in the window header, and
**Browse** in the bottom navigation and **Find wallpapers** in the menu both
open the same main-window tab.

The two sites are not alike, and the difference shows up everywhere below.
Wallhaven has a documented JSON API, so the work is in refusing to believe the
JSON it returns. MotionBGS has no API at all, so its provider reads public HTML,
which is why most of the code behind it is defence rather than parsing.

| | Wallhaven | MotionBGS |
|---|---|---|
| serves | stills (JPEG, PNG) | video wallpapers (MP4) |
| reached through | `https://wallhaven.cc/api/v1` | scraped HTML from `https://motionbgs.com` |
| credentials | optional; only NSFW results need a key | none |
| request spacing | 2 s between API calls | 1 s between requests |

## What a search actually does

Searches and downloads both run on worker threads and come back to the window
through `GLib.idle_add`, because a search takes as long as the site takes. The
two have separate single-worker pools: a 40 MB video download must not hold up
the next search, and two searches at once would only fight over the cache.

Each answer is capped at 48 results per page whatever the site returns, and each
provider keeps a small in-process cache of recent pages with a fifteen-minute
expiry. Paging back to somewhere you have already been is answered from that
cache rather than re-fetched, and the summary line at the bottom of the Browse tab
says `cached` when it was.

That summary line is worth reading:

```
24 results - of about 1130 - page 2 - 3 unreadable - cached
```

`unreadable` counts results the provider returned that we refused to normalise.
It is shown rather than quietly swallowed because a sudden jump in that number
means the remote's schema or markup has moved, and a mysteriously short grid is
a much worse way to find that out.

Failures arrive as a toast reading `kind: message`, where the kind is the
machine-readable half of a `ProviderError` — `credential`, `rate-limit`,
`challenge`, `site-markup`, `redirects`, `size-limit` and so on. The kind is
what the code branches on; the message is for you.

Previews come from the sites' own CDNs, fetched by up to four workers, bounded
at 4 MB each with a ten-second timeout, and cached by URL for the life of the
tab. MotionBGS serves WebP, which this closure's GdkPixbuf cannot decode, so
previews are transcoded through ffmpeg before they reach GTK. Without ffmpeg
installed those cards keep a blank frame; the download button still works.

The Browse tab is a single instance. Pressing the search button again while it is
open re-presents the one that exists, keeping its results and its preview cache
instead of throwing away a page of downloads. Closing it does not cancel a
download in flight: the provider stages bytes under a temporary name and links
them into place at the end, so an interrupted one leaves nothing behind and a
finished one is already in the library.

## Browsing without the window

The same three operations are on the control socket, so a running instance can
be searched and downloaded from without opening the window:

```
wall-in-one ctl providers
wall-in-one ctl search <provider> [query]
wall-in-one ctl download <provider> <identifier> [hd|4k]
```

The query is everything after the provider name, spaces and all, so quoting it
is optional: `ctl search wallhaven aurora over the fjord` is one query, not four
arguments. The variant on `download` is MotionBGS's quality; left off, the
provider takes the best it is offered, which is what the Browse tab's download
button does too.

Output is **one row per line with tab-separated fields**, and everything that is
not a row is a `#` comment. That is the format both readers of it already
understand: a person sees columns, and `cut -f1`, `awk -F'\t'` and
`while read -r id kind rest` get the fields out with nothing installed. Tabs
rather than spaces because a wallpaper title is full of spaces and would
otherwise read as several columns; a tab or a newline *inside* a title collapses
to a space before it is printed, so a website cannot invent a column or a row in
a script's input.

```
$ wall-in-one ctl search wallhaven aurora
# wallhaven: 24 results - of about 1130 - page 1
# fields: identifier, kind, resolution, title
o5jvv1	still	3840x2160	Aurora over the fjord
zyw8kg	still	1920x1080	Northern lights
...

$ wall-in-one ctl download wallhaven o5jvv1
downloaded wallhaven-o5jvv1.jpg (2.4 MB) -> /home/you/Pictures/Wallpapers/Wall-in-One/Wallhaven/wallhaven-o5jvv1.jpg
```

The summary comment is word-for-word the one under the Browse grid, including
`unreadable` and `cached`. An empty field prints as `-`, and a result with no
title prints its identifier instead, exactly as the cards do. `ctl providers`
prints `name, media, usable, limitations` the same way, which is how a script
finds out that NSFW results are unreachable before asking for them.

Only the first page is reachable: the protocol carries one argument per request,
and spending it on a page number would cost the query its spaces. There is no
filter surface either — categories, purity, sorting and MotionBGS's browse modes
are the Browse tab's, not the socket's.

**Nothing blocks the window.** The control server answers from the GTK main
loop, so a handler that waited for a website would freeze every frame the app
draws for as long as the site took. `search` and `download` therefore answer
*later*: the verb hands its work to a single-worker pool, returns without a
response, and the reply is written when the worker comes back through
`GLib.idle_add` — the same arrangement the Browse tab uses, for the same two calls.
The client's connection simply stays open until then, which costs one file
descriptor and keeps `ctl search` an ordinary blocking command that prints its
results. `ctl` allows a minute for a search and ten for a download, against five
seconds for every other verb.

A failure comes back on stderr with a non-zero exit, reading `kind: message` —
the same sentence the Browse tab toasts. The kind also travels as its own field in
the reply, so a client can branch on `rate-limit` without parsing the English
next to it. An unreachable network is a failed response, never a traceback and
never a dead app: whatever the transport raises is caught on the worker and
turned into one.

Downloads from here are indistinguishable from downloads from the Browse tab. They
run the provider's own install path, so the directory marker and the per-file
sidecar come out identical, and they land in the first configured root for the
same reason the Browse tab's do. A finished one rescans the library, so the file is
in the grid of an open window without being asked for.

## Driving the library from the socket

The verbs above fill the library. These six read it, and are documented here
because they answer in the same rows and are read by the same `awk`:

```
wall-in-one ctl list [everything|stills|videos|favourites] [query]
wall-in-one ctl select <path>
wall-in-one ctl favourites
wall-in-one ctl favourite <path>
wall-in-one ctl unfavourite <path>
wall-in-one ctl remove <path>
```

`list` prints `path, kind, ownership, favourite`. The kind word comes first and
everything after it is the query, spaces and all, exactly as `search` reads a
provider off the front — so `ctl list videos snow village` is one query. A first
word that names no kind is a usage error rather than a query, because
`ctl list videos` would otherwise be ambiguous. The selecting, the matching and
the ordering are `library/filter.py`'s, the same code behind the window's search
box and kind dropdown, so the two cannot come to disagree about which wallpapers
a word names.

```
$ wall-in-one ctl list videos snow
# library: 2 of 634 videos matching "snow"
# fields: path, kind, ownership, favourite
/home/you/Pictures/Wallpapers/snowy-village.mp4	video	user	yes
/home/you/Pictures/Wallpapers/Wall-in-One/MotionBGS/snowfall.4k.mp4	video	managed	no
```

The `ownership` column is what says which of the two things `remove` would do to
that file. It is the same distinction as ["What makes a file
ours"](#what-makes-a-file-ours): `managed` is a file this app downloaded and
`user` is one of yours.

`list` reports the scan the window is already showing rather than re-reading the
disk; the refresh button and a finished download are what re-read it. One reply
has to fit in a single 64 KB message, so a very large library is cut between two
rows and the summary says how many did not fit — narrow it with a query. A
wallpaper whose *name* contains a tab or a newline cannot be printed in a
tab-separated row at all without printing a path that would no longer work, so
it is counted as `unlistable` instead of being shown mangled.

### Paths are resolved, not trusted

Every verb above that takes a path looks it up in the library and refuses
anything it does not find, with the kind `not-in-library`. The path must be
absolute: this socket is answered by the window's process, whose working
directory is wherever the app was launched from and almost never where you are
standing, so a relative one would quietly name a different file. A leading `~`
is expanded. The match is exact — `/w/../w/a.png` is refused rather than
normalised, because deciding that two strings name one file is a decision worth
getting wrong nowhere, and least of all in front of `remove`. The paths `list`
prints are the ones the scan produced, so copying one out of it always works.

### `remove` destroys something

There is no confirmation over a socket, so the refusals in `library/manage.py`
are the whole of the protection, and the socket adds none of its own and takes
none away. There is no force flag and no way to ask for the other verb.

```
$ wall-in-one ctl remove /home/you/Pictures/Wallpapers/Wall-in-One/Wallhaven/wallhaven-o5jvv1.jpg
removed wallhaven-o5jvv1.jpg and 1 file beside it - deleted, which cannot be undone

$ wall-in-one ctl remove /home/you/Pictures/Wallpapers/holiday.png
holiday.png moved to the trash - /home/you/.local/share/Trash/files/holiday.png
```

Which of those two happened is in the reply, because only one of them can be
undone. A downloaded wallpaper is unlinked along with the sidecar and any still
this app generated for it; one of your own is moved to the freedesktop trash,
where your file manager can put it back. Ownership is re-derived from disk at
the moment of deletion rather than taken from the listing, which may be minutes
old, and a file that fails that check comes back refused with the kind
`not-ours` and is left exactly where it was. The wallpaper also loses its star,
and the open window rescans, so no tile outlives the file it was drawing.

### Favourites

`favourite` and `unfavourite` write the same starred list the tiles show — the
running app's own, so a star set from a terminal appears in the window and
narrows the rotation when **Cycle favourites only** is on.

The two are deliberately not symmetrical. `favourite` needs a wallpaper the
library has; a star on something we cannot see would be a line in a file with no
tile and no rotation entry. `unfavourite` takes any absolute path, because a
favourite whose file is not here — an unmounted drive, a root removed from the
settings — is kept rather than pruned, and taking the star off by hand is the
only thing left to do with one. Requiring a lookup there would make the entries
most worth removing the ones you could not remove.

`ctl favourites` prints those entries in the order they were marked, with a
`present` column saying whether the last scan found each one. That is a
different question from `ctl list favourites`, which can only show what is here.

```
$ wall-in-one ctl favourites
# favourites: 3 starred - 1 not in the library right now
# fields: path, present
/home/you/Pictures/Wallpapers/snowy-village.mp4	yes
/media/photos/aurora.jpg	no
```

A write that fails comes back as a failure with the kind `local-io`, and the
star still moves: the app keeps it for this session and is telling you it will
not survive the next launch.

## Wallhaven filters

The provider accepts exactly these option keys (`FILTER_OPTIONS` in
`providers/wallhaven.py`) and rejects any other key with an `invalid-request`
error rather than ignoring what may well be a typo.

| option | accepted values | default |
|---|---|---|
| `categories` | three bits, general/anime/people, e.g. `110`; `000` is refused | `111` |
| `purity` | three bits, SFW/sketchy/NSFW; `000` is refused | `100` |
| `sorting` | `date_added`, `relevance`, `random`, `views`, `favorites`, `toplist`, `hot` | `date_added` |
| `order` | `asc`, `desc` | `desc` |
| `atleast` | one `WIDTHxHEIGHT`, each side 1–100000 | none |
| `resolutions` | up to twelve exact `WIDTHxHEIGHT`, comma-separated | none |
| `ratios` | up to eight `WIDTHxHEIGHT`, each side 1–1000 | none |
| `colors` | one of Wallhaven's 29 documented hex values, with or without `#` | none |
| `top_range` | `1d`, `3d`, `1w`, `1M`, `3M`, `6M`, `1y` | `1M` |
| `seed` | six alphanumeric characters | none |

Four rules are enforced before any request is made, because each of them
otherwise produces results that do not match what was asked for:

- `atleast` and `resolutions` cannot both be set — one is a floor and the other
  is an exact list, and Wallhaven would silently honour only one.
- `top_range` is only sent when `sorting` is `toplist`, and `seed` is only
  accepted with `sorting` set to `random`.
- The third purity bit is refused outright without an API key. Sending it
  unauthenticated gets a silent downgrade to SFW and an empty-looking grid with
  no explanation.
- Query text is at most 256 UTF-8 bytes and must be free of control characters;
  `page` runs from 1 to 10000.

A comma-separated list must round-trip unchanged after normalisation, so
`1920x1080,` and `01920x1080` are errors rather than filters that quietly become
something else.

The Browse tab exposes four of these: sort, categories, rating, and a minimum size
box that fills in `atleast`. The rest are reachable from `SearchQuery.options`
in code; there is no settings key or command-line flag for them.

## MotionBGS browse modes

MotionBGS has five modes (`MODES` in `providers/motionbgs.py`): `search`,
`latest`, `genre`, `4k`, `hd`. With nothing specified the mode is `search` when
there is query text and `latest` when there is not.

The constraints below are not this app's policy; they are what the site does,
established by observation rather than from documentation, and enforced locally
so a request that cannot work fails with an explanation instead of returning
someone else's page.

- **Text search normally pages through its tag catalogue.** The site redirects
  searches such as `naruto` to `/tag:naruto/`; Wall-in-One follows that one
  same-origin redirect, validates the final path as exactly
  `/tag:<lowercase-slug>/`, and uses `/2/`, `/3/` and later pages as the result
  grid scrolls. If a query does not resolve to a tag route, its direct result is
  still shown but honestly remains one page.
- **HD browsing is first page only.** `/hd/` is a single curated page.
- **`latest`, `genre` and `4k` are paged.** Even there, the previous and next
  buttons are only enabled when the page's own `rel="prev"` / `rel="next"` link
  resolves to the route we would have asked for. A pagination link pointing
  anywhere else is ignored.
- **A genre is valid only in genre mode**, and must be a lowercase MotionBGS
  slug. Passing `genre` in any other mode is an error, as is passing query text
  in any mode other than `search`.

The Browse tab's mode dropdown offers Latest, 4K, HD and Genre — `search` is not
listed, because typing into the box is what selects it. Whenever the box is
non-empty the request goes out as a search regardless of the dropdown, since
MotionBGS rejects a query and a browse mode together. The genre field only
appears when Genre is selected.

Two failure modes are told apart deliberately. A page that says "no wallpapers"
is an empty result; a page with no recognisable cards and no such wording is
reported as `site-markup`, meaning the public markup has probably changed. An
anti-bot challenge is reported as `challenge`, and no bypass is attempted.

Each card carries the quality badge from the listing, `hd` or `4k`, and that is
the file downloaded. Where the listing did not say, the detail page's best
option is taken: 4K first, then HD — a video wallpaper is worth the bytes.

## Where downloads land

Everything goes under one `Wall-in-One` directory in the first library root, so
a whole install is one directory to inspect or delete. That root is Noctalia's
`[wallpaper] directory` when Noctalia's settings name a real directory,
otherwise `~/Pictures/Wallpapers` or `~/Pictures`, whichever exists first. With
none of those present a download fails with a `no-root` error rather than
inventing a location.

| | Wallhaven | MotionBGS |
|---|---|---|
| directory | `<root>/Wall-in-One/Wallhaven/` | `<root>/Wall-in-One/MotionBGS/` |
| file | `wallhaven-<id>.jpg` or `.png` | `<slug>.hd.mp4` or `<slug>.4k.mp4` |
| sidecar | `<file>.wallhaven.json` | `<file>.motionbgs.json` |
| directory marker | `.managed-by-wall-in-one-v1.json` | `.wall-in-one-motionbgs-managed.json` |

Those four names are inherited from the previous implementation rather than
chosen, so a library downloaded by it keeps its ownership across the rewrite.

The **sidecar** records where a file came from: the provider, the source page,
the URL the bytes actually arrived from, the byte count, a SHA-256 of the file,
and a UTC timestamp. The **directory marker** records only that this app created
the directory.

Installation never overwrites. The staged download is hard-linked into place
alongside a freshly written sidecar, and `os.link` fails rather than replacing an
existing name; if either link fails the other is rolled back, so the library
never sees a media file without its provenance or the reverse. Wallhaven refuses
with a `conflict` error when the file is already there, since one id means one
file. MotionBGS counts up instead — `<slug>.4k-1.mp4` — and treats a name as
free only when both the media file and its sidecar are absent, because a sidecar
with no media means an interrupted install and reusing that name would attach
the wrong provenance to new bytes.

## What makes a file ours

The library scan calls a file `Ownership.MANAGED` only when **both** are true:

1. its directory carries one of the two directory markers, and
2. the file itself has an adjacent sidecar.

A marker on its own is never enough. That is the whole point: a wallpaper you
copied into `Wall-in-One/Wallhaven/` by hand has no sidecar, so it stays
`Ownership.USER`. Ownership is what decides whether a file counts as deletable
by the app, and it is also what puts the provider's name on a tile as a badge in
the library grid — an unbadged tile in a managed directory is yours, not ours.

The scan skips dot-files entirely, and downloads are staged under a dot-prefixed
temporary name, so a staged file left behind by a hard kill is invisible to the
library rather than showing up as a broken wallpaper.

## The Wallhaven API key

Wallhaven works unauthenticated. A key adds exactly one thing: NSFW results.

The key is resolved in this order, first hit wins:

1. a key passed explicitly by the caller,
2. the `WALLHAVEN_API_KEY` environment variable,
3. `~/.config/wall-in-one/wallhaven-api-key` (or the same file under
   `$XDG_CONFIG_HOME/wall-in-one/`).

The file exists so the key need not sit in the environment of every process you
start. It is read as one line and nothing else, and it is ignored — as though
absent — if it is a symlink, not a regular file, larger than 4096 bytes, or if
its first line is not 1–256 characters of `A-Za-z0-9_-`. A malformed key file
must not stop the app starting, so Wallhaven simply runs unauthenticated and
reports that as a limitation, which is what greys out the NSFW checkbox in the
filter popover and gives it a tooltip saying why.

The key travels as an `X-API-Key` request header. It is never put on a command
line: the previous implementation shelled out to `curl` and had to pass the key
through a header file to keep it out of `argv` where any local process could
read it, and that whole problem disappeared along with the subprocess.

A key that the site rejects comes back as a `credential` error, distinct from
having no key at all.

## What the network layer refuses

All provider traffic goes through one small client (`providers/http.py`), which
is also the only place under `providers/` that imports `urllib`. The properties
below are deliberate, and each replaces something the predecessor got from a
`curl` flag.

- **HTTPS is enforced before a socket opens.** A URL is rejected unless its
  scheme is `https`, it has a host, it carries no username or password, and its
  port is absent or 443. Providers have already validated URLs against their own
  origin by then; this is the backstop for the ones that have not, and for a
  `Location` header a provider forgot to re-check.
- **Redirects are declined, not followed.** A 3xx comes back as an ordinary
  response carrying its `Location`, and the provider decides what to do — which
  is how a cross-origin redirect fails closed. Wallhaven treats any redirect as
  an error, because its API does not redirect and one that does is not it.
  MotionBGS follows at most three hops by hand, re-anchoring each `Location` on
  the current URL and re-validating it against the MotionBGS origin, then checks
  that the page it landed on is the page it asked for.
- **Every response is bounded by a byte ceiling**, enforced while reading rather
  than trusted from `Content-Length` — a declared length over the ceiling is
  refused before the body is read, and a lying one is caught as it arrives.
  Search JSON is capped at 512 KB, MotionBGS HTML at 1 MB, previews at 4 MB, a
  MotionBGS video at 512 MB, and a Wallhaven image at exactly the size the API
  advertised for it.
- **Previews that are not images are discarded.** Anything that is not a 200
  with an `image/*` content type yields no preview at all, so an error page or a
  redirect body becomes a card without a picture rather than something handed to
  a decoder.
- Only GET is ever issued. There is no other verb in the client; providers only
  read.

Downloaded bytes are then checked against the metadata that authorised fetching
them. A Wallhaven image must match the advertised byte count and MIME type, and
its dimensions must match when read out of the file's own JPEG or PNG headers —
parsed structurally, never a pixel decoded. Its CDN URL must additionally be the
derived `w.wallhaven.cc/full/<shard>/wallhaven-<id>.<ext>` shape with the shard
and id agreeing with the record carrying it; anything else is dropped as a
result that could point the downloader at another host's file. A MotionBGS
transfer must have resolved inside the media route the detail page authorised,
for the same media id, and must begin with an ISO-BMFF `ftyp` box — the MIME
type is the remote's opinion, the box is the file's.

MotionBGS markup is parsed with ceilings on tags, attributes per tag and bytes
per attribute, so a hostile page cannot exhaust memory during the parse, and
every URL taken out of it is re-anchored on the MotionBGS origin with
percent-encoded separators decoded first, because `%2e%2e%2f` is a traversal and
`urlsplit` will not tell you so. Filenames derived from remote metadata are
resolved and refused if they could leave their directory.
