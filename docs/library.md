# The library

The Library shows your wallpapers, their representative stills and colour
choices, and your favourites. This page covers finding, editing and removing
items, followed by the storage and recovery guarantees behind those actions.

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

With none listed, the library is not configured. On first graphical launch the
app shows the exact directory detected from Noctalia (or the local fallback)
and asks whether to use it or open a folder chooser. Detection is a suggestion,
not permission to write: until a root is chosen the scan is empty, downloads
are refused, and generated stills stay disabled. Choosing either route saves
the root, so configured users are not asked again.

Three properties of the list are worth knowing:

- **Order is kept, and the first one is special.** Downloads and generated
  stills land under the first root. It is marked in the Settings tab with
  a download icon and **Downloads & generated stills**.
- **Duplicates are dropped.** Paths are expanded and made absolute first, so
  `~/Pictures` and `/home/you/Pictures` count as one entry rather than putting
  every wallpaper in it into the rotation twice.
- **A root that is not there right now is kept, not removed.** An unmounted
  drive shows in the Settings tab as `-- not there right now` and comes
  back when the drive does.

Scanning may still show files reached through an intentionally configured
symbolic link, but Delete and Move to Trash refuse destructive authority
through symbolic-link directory components. Configure the real absolute root
when you want those actions available. This keeps a link swapped between scan
and removal from redirecting an operation outside the selected library.

Changing the list rescans immediately. Removing a folder from this list stops
scanning it; it does not delete the wallpapers in it.

## Videos and the stills behind them

A video wallpaper is played by mpvpaper, but the paired still is set through
Noctalia first. Two things follow from that: if the renderer dies the right
image is still on screen, and Noctalia's generated palette matches what the
video looks like rather than whatever was up before it.

A video finds its still three ways, in this order:

1. a `<video>.wall-in-one.json` sidecar naming one;
2. its deterministic path-keyed file under
   `<each configured root>/Wall-in-One/Automatic Stills/`, searched in root
   order;
3. a sibling named `foo-still.png` -- or plain `foo.png` -- next to `foo.mp4`.

Only the exact deterministic capture in `Automatic Stills` is absorbed by its
moving wallpaper. A sibling or manually selected image is user-provided and
remains a separate library item with its own Pairing, even while one or more
moving wallpapers use it as their representative.

New automatic captures are written under the first configured root. Existing
deterministic captures are discovered under every configured root so changing
root order does not strand an older representative.

An explicit representative choice must already be a still item in the latest
library scan. The picker and `wall-in-one ctl still` refuse an outside file, an
unindexed file, or an indexed video. Wall-in-One does not silently import,
copy, move, or later delete an external picture. To use one, copy or move it
into a configured library folder, or add its folder under **Settings**, refresh
the library, and choose the newly indexed still. Automatic generated captures
remain internal defaults rather than separate choices in the picker.

### Stills the app makes for itself

The app tries to capture a still for each video that lacks one. It takes a
full-resolution PNG three seconds in to avoid opening fades, and records the
pairing in a sidecar when the video itself is inside the target library root.
Videos from another configured root still use the deterministic managed still;
the app does not write a pairing file beside a source outside that target root.

Generated filenames include a digest of the video's absolute path. Two files
named `intro.mp4` in different folders therefore get different stills rather
than silently overwriting each other.

- Captures run after each rescan on a single background worker.
- Each video is attempted once per session, so a failed capture does not keep
  retrying on every rescan.
- It needs `ffmpeg`. The Nix package puts it on the app's `PATH`; without it
  the video still plays and simply keeps its "Video (no still)" badge.
- Switching dynamics off takes a still from the video playing at that moment
  first, so the wallpaper you were watching is not swapped out from under you.

### Capture publication guarantees

Those deterministic names form an app-reserved namespace. Rendering reads the
retained source and writes one pre-created hidden output inode through
descriptor-backed paths beneath the pinned `Automatic Stills` directory. Any
public target already present is pinned before the long renderer call.
Publication rechecks the source, output, parent and frozen target before a
no-replace move. A target which appeared after rendering began, or whose
original inode changed in place, wins and is preserved. A hard kill may leave
only the inert hidden output; it cannot redirect the renderer into an unrelated
public file.

## Finding one

The row under the header bar is a search box, a kind filter, and a sort. All
three work over the complete scanned inventory without rescanning the
filesystem. Search, kind and sort reset between launches, so the next launch
starts with the full library visible.

The grid initially materialises 72 tiles and thumbnail requests, then offers a
labelled **Load more** page. Matching and sorting still cover all 4096 supported
items: an exact search for the last item loads that result immediately rather
than requiring 56 page clicks. This bounds GTK widget and thumbnail work while
keeping the complete library reachable. Loaded pages, surviving tile objects,
the scroll adjustment and keyboard focus survive rescans. A thumbnail is
requested when its tile is first materialised; the on-disk cache prevents a
repeat decode unless the media changed.

**Search** splits what you type on whitespace and requires every word to appear
somewhere in the filename stem, in any order. `snow vil` finds
`snowy-village-still`. Matching folds accents and case, so `cafe` finds `Café`.
Folder names and extensions are not searched. Escape clears the box.

**Kind** offers Everything, Stills only, Videos only, and Favourites only.

**Sort** offers Name, Newest first (modification time), and Largest first.
Ties fall back to name and then path, so the grid does not reshuffle equal-sized
files on every rescan.

While anything is being hidden the window's subtitle says both numbers, as
`Showing 3 of 41` in front of the usual counts -- 41 being how many wallpapers
are playable, not how many are on screen. Sorting alone does not narrow the
results or trigger that count.

## Favourites

Use the star in the corner of a tile to mark a favourite. Favourites are saved
separately from settings in `~/.local/state/wall-in-one/favourites.json`.

Each change is written through immediately, in the order you starred things.
If the write fails the star stays where you put it and a toast says it will not
outlive the session.

**A favourite whose file is not in the library is kept.** Removing a root from
the scan, disconnecting a drive or reaching the scan limit does not clear its
star. Unstarring the item or explicitly removing its wallpaper clears it.

**Settings -> Playback -> Cycle favourites only** narrows the rotation to them.
If no favourites are available to play, rotation falls back to the whole
library.

## Using or taking away an item

Each Library tile separates editing from applying:

- **Left-click** or **Edit** opens that item's full pairing editor: representative still,
  moving source and palette policy.
- **Apply** plays the pairing through the one-entry **Quick choice** playlist.
  Independent-display mode offers explicit display targets before applying.
- **Right-click** or the **actions button** opens the keyboard-reachable menu
  for Quick choice, favourites, representative still, colours, reset and
  removal. Opening the menu does not apply the wallpaper. Entries which do not
  apply to that item are omitted.

The editor's adaptive colour choices offer all ten Noctalia generators with
previews. Community and custom palettes show their stored colours. Built-ins
have no colour preview because Noctalia does not expose their colours without
applying them.

The last menu entry is **Remove** or **Move to Trash**.

- **Remove** appears for a downloaded file whose app ownership can be verified.
  It permanently deletes the wallpaper and its verified app-owned sidecars and
  generated stills. It asks for confirmation and cannot be undone through Trash.
- **Move to Trash** appears for your own files inside a configured library
  root. It moves them to the freedesktop home trash under
  `~/.local/share/Trash`, where a file manager can restore the media. It does not
  ask for confirmation. Restoring the file does not restore the app metadata
  cleared by removal, such as favourites and playlist occurrences.

Only the home trash is implemented. A wallpaper on another filesystem cannot be
renamed into it: the action reports the problem and leaves the file in place.

### Ownership and removal guarantees

Permanent removal pins the downloaded media and its provider sidecar before
the commit, together with every generated still/pairing sidecar pair found for
it under the configured roots. Only those exact app-owned companions can be
physically removed by that operation.

A file is only ours when two things agree: a marker file says we made the
directory, and a bounded regular provider sidecar names the exact provider,
logical path and final media generation. That generation contains device,
inode, byte count, modification time and change time. The scan compares all
five fields; irreversible Remove also hashes the exact retained media inode and
requires the sidecar's byte count and SHA-256 before withdrawing that authority.
Anything which fails one check stays yours. A `.wall-in-one.json` pairing
sidecar only records the chosen representative still; it never grants deletion
authority. Ownership is re-checked on disk at the moment of deletion, not
trusted from a scan that may be minutes old.

Provider installs first move the media into place without replacement and sync
its directory. They then capture and hash that exact post-rename generation,
write and sync its bound sidecar, recheck the named media, and publish the
sidecar without replacement. Cancellation is not accepted after the media
commit. A kill, conflict or local error in that interval can leave visible
media, but without its exact authority it scans as user-owned and can only be
moved to Trash. Hidden staging files and unmistakable predecessor orphan
sidecars with no media are removed after 24 hours; recent entries, symlinks,
unknown files and visible media are left alone.

Trash metadata names are reserved with no-replace links, then the exact
journaled media generation is moved to the matching Trash name with Linux's
atomic no-replace rename. A live no-follow inode reference spans preparation
through the move, and the journal also records size and change timestamps, so
even immediate reuse of the same device/inode numbers cannot authorize a
replacement. Concurrent files with the same basename therefore receive
distinct names; neither a same-path replacement nor an existing Trash entry
can be overwritten. Both the moved bytes and `.trashinfo` record are synced
before the operation is reported durable.

Removing a wallpaper also drops it from favourites, every playlist occurrence,
and its complete Pairing record: palette policy, representative-still choice
and any recorded playback-failure information. It removes only automatic stills and
sidecars whose exact generated paths prove they belong to that wallpaper.
Those records normally outlive a missing file because the file might come
back; that is not true of one the app just destroyed.

A manually chosen still remains a separate library item. Removing the moving
wallpaper deletes the association but never the still file or that still's own
Pairing metadata. Conversely, explicitly removing the still clears only the
representative-still field in other Pairings which selected it; their palette
and health remain, and they fall back to automatic/default still selection.
An unavailable drive or temporarily missing file is not an explicit removal
and does not trigger either cleanup path.

Before a local deletion or trash move can touch the media, Wall-in-One writes a
bounded removal intent to `pending-removals.json`. If that intent cannot be
persisted, the operation is refused and the file stays where it was. The
journal binds a unique transaction token to the pinned source generation and
the selected library-root/source-parent identities. A replacement at the same
path is refused, another process cannot borrow or cancel the live transaction,
and an unmarked missing source is not interpreted as a completed delete while
its drive is unavailable. Both the live Delete/Trash commit and later crash
replay retain descriptor-backed references to that exact root and source
parent; a renamed, unmounted or replaced public root therefore cannot redirect
the source or its private claim.

Every visible provider, pairing and generated artifact which the physical
operation may remove is pinned before the media commit. Cleanup acts only
through those exact retained generations. A crash replay has no surviving
artifact pins, so it clears durable favourites, Pairings and playlists but
never rediscovers a same-name physical companion which could belong to a later
installation. A live cleanup failure is reported with the preserved paths; it
is not converted into authority to delete them on a later refresh.

A crash-left private media claim is replayed only when its journaled generation
still matches. Before truncating a singly linked regular inode, Wall-in-One
persists a generation marker through the exact writable descriptor; replay can
therefore distinguish an already-consumed zero tombstone from an arbitrary
empty replacement. Linux has no inode-conditional unlink, so exact residues
are moved under a private `.wall-in-one-retained/entry-*` namespace instead:
singly linked regular files are zeroed, while multiply linked files and sockets
remain intact. The scanner ignores this hidden namespace. If durable marking or
safe centralisation fails, the exact private claim or temporary is preserved.
Linux also cannot create a directory and return its descriptor atomically, so a
pin first obtained after directory creation grants anchored access, not proof of
ownership. Wall-in-One never relocates or removes a container on that basis:
transient deletion-claim containers begin in the retained namespace, durable
claim-token containers remain beside their source, and renderer IPC namespaces
remain inert under the runtime directory. New template transactions use only
atomically created regular sibling entries; recovery reclaims independently
proven children from older transaction directories but leaves each container
inert. A kill before the fixed transaction locator is published may leave
random, mode-0600 candidate/lock/record stages; they are inert and are not
guessed away by name. Cleanup never uses a pathname-only `rmdir` or treats a
post-creation directory pin as relocation authority.

The journal survives a crash after the media operation commits and is cleared
only after durable authored metadata is clean. If metadata cleanup fails, the
UI reports the partial failure and each GUI startup or library refresh retries
it. Fix the reported state-directory problem and refresh to finish that
cleanup. The headless runtime-config compiler checks the journal for damage but
does not mutate authoring state from its short-lived snapshot.

Automatic-still rendering does not hold deletion hostage for the duration of
ffmpeg or a scene capture. Rendering happens outside the per-item process
locks; only the short final publication and pre-commit artifact pinning share
them. Publication rechecks the exact opened source before a no-replace move. If
removal wins first, the publisher observes the missing or changed source and
discards its private capture rather than resurrecting a deterministic still.

An item marked **Playback unavailable** after three failed automatic hand-over attempts, or an
attributable Wallpaper Engine scene crash, is visibly labelled and has no
Quick-choice playback action. Delete/Trash is its recovery boundary:
the health marker is part of the Pairing record removed with the item, so a
later reinstall starts clean. Workshop items remain Steam-owned; Wall-in-One
clears their metadata only after a scan sees the concrete item absent while
the Workshop content root is still mounted, never merely because that drive or
library is unavailable. A transient malformed project or missing video entry
does not erase the process's last-known installation, so a later observed
directory removal can still be recognized. Because Steam has already committed
that external uninstall, Wall-in-One records and retries authored metadata
cleanup only. It never deletes Steam content or rediscovers deterministic
stills and sidecars for an external uninstall. If the removal journal is
unwritable, an in-process retry remains and the state-directory failure is
reported until a refresh can record the metadata boundary durably.

Workshop absence is intentionally not inferred from Pairings after a restart:
scene Pairings do not retain the last content root, video Pairings do not carry
trusted Workshop provenance, and uncustomized installs may have no Pairing at
all. An uninstall that happens entirely while Wall-in-One is stopped therefore
cannot yet be distinguished safely from an unavailable Steam library. Once an
observed uninstall reaches the removal journal, its cleanup is restart-safe.

## Playlist behavior and limits

The Playlists tab adds pairings from the searchable Library list with **+**,
double-click, Enter or drag-and-drop. Drag a playlist row's handle to reorder
it, or focus the row and press `Ctrl+Up` / `Ctrl+Down`. Reordering preserves
each occurrence's stable entry ID, so the same pairing can appear more than
once without those occurrences becoming interchangeable.

A Library row with playback disabled remains visible with its diagnostic, but
its Add button and drag source are disabled. An older playlist occurrence is
retained and labelled unavailable so it can be removed deliberately; playback
skips it when another usable occurrence exists and is disabled when every
entry has playback disabled or is missing.

Playlists can contain up to 10,000 entries. The order pane initially builds
72 rows and offers a labelled **Load more** page; loaded rows keep their
identity for keyboard and drag reordering. Clicking Add for an item appended
beyond the loaded range temporarily keeps that new row visible to confirm the
addition. The stored order is complete throughout: paging limits interface
work, not the runtime playlist or the entries written to disk.

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

## State-file recovery

If favourites, Pairings, playlists, schedules or display assignments contain
unreadable JSON, a later repair mutation first moves the exact generation that
failed to a numbered `.broken` file. Recovery publishes without replacement:
if an editor installs a valid document before or during that boundary, the
manual repair remains canonical and the app reports its own mutation as
failed. The same rule applies to an in-place repair of the original inode.

## Where things live

| path | what |
|---|---|
| `~/.config/wall-in-one/settings.toml` | settings (see the README) |
| `~/.config/wall-in-one/wallhaven-api-key` | the stored Wallhaven key, 0600 |
| `~/.local/state/wall-in-one/favourites.json` | the stars |
| `~/.local/state/wall-in-one/pending-removals.json` | crash-safe removal intents retained until durable authored metadata is clean |
| `~/.local/state/wall-in-one/palette.json` | where Noctalia renders the live palette |
| `~/.cache/wall-in-one/thumbnails` | the thumbnail cache |
| `$XDG_RUNTIME_DIR/wall-in-one.sock` | the control socket, 0600 |
| `$XDG_RUNTIME_DIR/wall-in-one-runtime.sock` | Rust runtime commands and status, 0600; falls back to `$XDG_STATE_HOME/wall-in-one/wall-in-one-runtime.sock` when unset |
| `$XDG_STATE_HOME/wall-in-one/runtime.toml` | atomically compiled, fully resolved service config |
| `<each configured root>/Wall-in-One/Automatic Stills/` | generated stills are discovered under every root; new captures use the first root |
| `<first root>/Wall-in-One/Wallhaven/`, `.../MotionBGS/` | downloads |
| `<affected parent>/.wall-in-one-retained/entry-*` | private exact cleanup residue; zero tombstones, intact retired transaction records/candidates, and empty transient claim containers are expected, while multiply linked files, sockets and unexpected evidence remain intact |
