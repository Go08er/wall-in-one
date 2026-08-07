# The library

The library is every wallpaper the app can see, and everything it knows about
them beyond the pixels: which still stands behind which video, which files are
ours to delete, and which ones you starred.

This page is the detail behind the README's summary. It is about what the app
does; [`DESIGN.md`](../DESIGN.md) is about why each decision went that way and
what it was checked against.

## What counts as a wallpaper

| kind | extensions |
|---|---|
| still | `.png` `.jpg` `.jpeg` `.webp` `.avif` `.gif` |
| video | `.mp4` `.mkv` `.webm` `.mov` `.avi` `.m4v` `.gif` |

`.gif` is in both lists on purpose and is classified as a video, because
mpvpaper animates it while a still wallpaper of an animated gif shows only the
first frame.

A scan is bounded: at most 4096 wallpapers, 65536 directory entries examined,
and 8 directories deep. Anything past that is reported in the window's subtitle
as skipped rather than silently dropped.

## Roots

Roots are the folders that get scanned, and they are the `roots` key in the
settings file -- **Settings -> Library** is the same list with an Add button.

With none listed, the app follows Noctalia's own `[wallpaper] directory`, so
the two agree about what the library is without configuring it twice. That is
the right default and the wrong thing to be stuck with: Noctalia has exactly
one, so a collection spread across two places used to be half invisible with no
way to say so.

Three properties of the list are worth knowing:

- **Order is kept, and the first one is special.** Downloads and generated
  stills land under the first root. It is marked in the settings dialogue with
  a download icon.
- **Duplicates are dropped.** Paths are expanded and made absolute first, so
  `~/Pictures` and `/home/you/Pictures` count as one entry rather than putting
  every wallpaper in it into the rotation twice.
- **A root that is not there right now is kept, not removed.** An unmounted
  drive shows in the settings dialogue as `-- not there right now` and comes
  back when the drive does.

Changing the list rescans immediately; nothing else notices otherwise, and a
folder you just added would stay invisible until the next launch.

## Videos and the stills behind them

A video wallpaper is played by mpvpaper, but the paired still is set through
Noctalia first. Two things follow from that: if the renderer dies the right
image is still on screen, and Noctalia's generated palette matches what the
video looks like rather than whatever was up before it.

A video finds its still three ways, in this order:

1. a `<video>.wall-in-one.json` sidecar naming one;
2. a file of the same stem under `<first root>/Wall-in-One/Automatic Stills/`;
3. a sibling named `foo-still.png` -- or plain `foo.png` -- next to `foo.mp4`.

A still that exists only to represent a video is not listed as a wallpaper of
its own, or the same picture would turn up twice in the rotation.

### Stills the app makes for itself

For a long time only the third route could actually happen: nothing wrote a
sidecar and nothing ever put a file in `Automatic Stills`, so a downloaded
video had nothing to show when dynamics were switched off, and pausing it
jumped to an unrelated wallpaper instead.

Now every video without a still gets one. The frame is taken three seconds in
-- videos routinely open on black or on a fade, and a black still looks like a
bug and generates a grey palette -- at full resolution, as PNG, and a sidecar
is written alongside recording the pairing.

- It happens after each rescan, on a single background worker. One, not four:
  each job is ffmpeg decoding a large video, so the disk is the limit rather
  than the cores.
- A video is attempted once per session. A finished batch causes a rescan and a
  rescan asks again, so a file ffmpeg cannot read would otherwise loop forever.
- It needs `ffmpeg`. The Nix package puts it on the app's `PATH`; without it
  the video still plays and simply keeps its "Video (no still)" badge.
- Switching dynamics off takes a still from the video playing at that moment
  first, so the wallpaper you were watching is not swapped out from under you.

## Finding one

The row under the header bar is a search box, a kind filter, and a sort. All
three work over what is already scanned -- nothing is re-read and no thumbnail
is regenerated -- and none of them is remembered between launches. A search is
about the next thirty seconds; reopening the app to yesterday's filter, with
most of the library missing and no obvious reason why, is a bug report.

**Search** splits what you type on whitespace and requires every word to appear
somewhere in the filename stem, in any order. `snow vil` finds
`snowy-village-still`, which plain substring matching does not. Matching folds
accents and case, so `cafe` finds `Café`. Only the stem is matched: searching
the whole path would mean any word that happens to name a directory matches
everything underneath it. Escape clears the box.

**Kind** offers Everything, Stills only, Videos only, and Favourites only.
Favourites is in this control rather than beside it because "which of these am
I being shown" has one answer at a time.

**Sort** offers Name, Newest first (modification time), and Largest first.
Ties fall back to name and then path, so the grid does not reshuffle equal-sized
files on every rescan.

While anything is being hidden the window's subtitle says both numbers, as
`Showing 3 of 41` in front of the usual counts -- 41 being how many wallpapers
are playable, not how many are on screen. Replacing the count instead would be
indistinguishable from a scan that had just lost most of the collection.
Sorting alone is not narrowing and does not trigger it.

## Favourites

The star in the corner of each tile. They live in
`~/.local/state/wall-in-one/favourites.json`, not in the settings file: three
hundred absolute paths would bury the dozen lines of a settings file that are
worth reading, and every star toggled from a tile would rewrite a file somebody
might be editing.

Each change is written through immediately, in the order you starred things.
If the write fails the star stays where you put it and a toast says it will not
outlive the session.

**A favourite whose file is not in the library is kept, not pruned.** A root
temporarily removed, a drive not mounted at startup, a scan that hit its
ceiling -- pruning on load would quietly forget a list you built by hand,
precisely when the app is least able to tell that anything is wrong. Only two
things drop an entry: you saying so, and the app itself destroying the file.

**Settings -> Playback -> Cycle favourites only** narrows the rotation to them.
It is ignored whenever that would leave nothing to rotate through: a manager
that stops changing the wallpaper is a worse answer to "you have no favourites
right now" than one that falls back to the whole library and keeps working.

## Taking one away

Each tile has an actions button in its corner, and the same menu opens on a
right-click anywhere on the tile:

- **Set as wallpaper** -- the same thing activating the tile does.
- **Remove**, or **Move to Trash**.

Which of the two you get is named for what it does to *that* file, because one
of them cannot be undone:

- **Remove** appears for a file we downloaded. It unlinks it along with
  everything we wrote beside it -- the sidecar proving we owned it, a still we
  generated for it, and that still's sidecar. It asks for confirmation.
- **Move to Trash** appears for your own files. It moves them to the
  freedesktop home trash under `~/.local/share/Trash`, where a file manager can
  restore them. It does not ask, because confirming everything trains people to
  confirm everything and this one is recoverable.

Only the home trash is implemented. A wallpaper on another filesystem cannot be
renamed into it and is refused with a reason, rather than being silently
unlinked when you expected to get it back.

A file is only ours when two things agree: a marker file says we made the
directory, and a per-file sidecar says we fetched that particular file. Both
are required, so anything you drop into a downloads folder by hand stays yours.
Ownership is re-checked on disk at the moment of deletion, not trusted from a
scan that may be minutes old.

Removing a wallpaper also drops it from the favourites. The reason favourites
outlive a missing file is that the file might come back, which is not true of
one the app just destroyed.

## Thumbnails

Every thumbnail is generated by ffmpeg, stills included, because this closure's
GdkPixbuf has no webp or avif loader and videos need a frame grab anyway. That
costs 230-330 ms per wallpaper, which is why the results are cached on disk at
`~/.cache/wall-in-one/thumbnails` -- a library you have not changed should cost
nothing to show on the second launch. A cached lookup is around 0.1 ms.

The cache is bounded at **256 MB**, evicted least-recently-used first down to
90% of the ceiling, with a one-minute grace so a thumbnail about to be drawn is
never thrown away. The cache key carries the source file's size and
modification time, so editing a wallpaper in place misses rather than serving a
stale picture. Every read is validated, so a truncated file is a miss rather
than a decode failure.

Two name shapes are written and therefore the only two that are ever deleted;
anything else you leave in that directory survives both eviction and a clear.
Deleting the whole directory is safe -- it is rebuilt on demand.

## Where things live

| path | what |
|---|---|
| `~/.config/wall-in-one/settings.toml` | settings (see the README) |
| `~/.config/wall-in-one/wallhaven-api-key` | the stored Wallhaven key, 0600 |
| `~/.local/state/wall-in-one/favourites.json` | the stars |
| `~/.local/state/wall-in-one/palette.json` | where Noctalia renders the live palette |
| `~/.cache/wall-in-one/thumbnails` | the thumbnail cache |
| `$XDG_RUNTIME_DIR/wall-in-one.sock` | the control socket, 0600 |
| `<first root>/Wall-in-One/Automatic Stills/` | generated stills |
| `<first root>/Wall-in-One/Wallhaven/`, `.../MotionBGS/` | downloads |
