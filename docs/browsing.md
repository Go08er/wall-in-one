# Searching and downloading wallpapers

Wall-in-One can pull wallpapers from two sites: **Wallhaven** for stills and
**MotionBGS** for video wallpapers. The search button in the window header, and
**Find wallpapers** in the menu, both open the same dialog.

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
cache rather than re-fetched, and the summary line at the bottom of the dialog
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
dialog. MotionBGS serves WebP, which this closure's GdkPixbuf cannot decode, so
previews are transcoded through ffmpeg before they reach GTK. Without ffmpeg
installed those cards keep a blank frame; the download button still works.

The dialog is a single instance. Pressing the search button again while it is
open re-presents the one that exists, keeping its results and its preview cache
instead of throwing away a page of downloads. Closing it does not cancel a
download in flight: the provider stages bytes under a temporary name and links
them into place at the end, so an interrupted one leaves nothing behind and a
finished one is already in the library.

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

The dialog exposes four of these: sort, categories, rating, and a minimum size
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

- **Text search is first page only.** The site's search route exposes no
  pagination, so asking for page 2 of a search is a validation error.
- **HD browsing is first page only.** `/hd/` is a single curated page.
- **`latest`, `genre` and `4k` are paged.** Even there, the previous and next
  buttons are only enabled when the page's own `rel="prev"` / `rel="next"` link
  resolves to the route we would have asked for. A pagination link pointing
  anywhere else is ignored.
- **A genre is valid only in genre mode**, and must be a lowercase MotionBGS
  slug. Passing `genre` in any other mode is an error, as is passing query text
  in any mode other than `search`.

The dialog's mode dropdown offers Latest, 4K, HD and Genre — `search` is not
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
