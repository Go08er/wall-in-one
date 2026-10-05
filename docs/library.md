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
and 8 directories deep. Anything past that is left out and counted as skipped
rather than silently dropped: the Library's count says `· 5 skipped`, and
names the first few when you point at it (the classic window's subtitle counts
them too).

## Roots

Roots are the folders that get scanned, and they are the `roots` key in the
settings file -- **Settings → Library** is the same list, with **Add folder…**
(the Library page's **+** button does the same).

With none listed, the library is not configured. On first graphical launch the
app shows the exact directory detected from Noctalia (or the local fallback)
and asks whether to use it, open a folder chooser, or decide later (the new
interface asks on its welcome screen, classic in a dialog). **Not now** saves
nothing; the question comes back at the next start, and the new interface's
notice under the header asks again before then. Detection is a suggestion,
not permission to write: until a root is chosen the scan is empty, downloads
are refused, and generated stills stay disabled. Choosing a folder either way
saves the root, so configured users are not asked again.

Three properties of the list are worth knowing:

- **Order is kept, and the first one is special.** Downloads and generated
  stills land under the first root. Settings marks it **Downloads & stills**;
  the download button on any other folder makes that one the first.
- **Duplicates are dropped.** Paths are expanded and made absolute first, so
  `~/Pictures` and `/home/you/Pictures` count as one entry rather than putting
  every wallpaper in it into the rotation twice.
- **A root that is not there right now is kept, not removed.** An unmounted
  drive shows in Settings as `<path> · not there right now`, with a warning
  icon, and comes back when the drive does.
- **A folder that playlists need stays.** Removing a folder, or turning off
  Wallpaper Engine scanning, takes its wallpapers out of the library. It is
  refused when that would leave a playlist something uses with nothing to
  play, or any playlist holding only wallpapers playback can't use, and the
  refusal names them and what each would lose. The change is tried for
  pretend with the real scan first, so what counts is what that scan finds
  (see [How a change is checked](#how-a-change-is-checked)): `/a/b` never
  holds `/a/bc`; a video or scene that stays counts too when its still goes,
  as a capture under a folder no longer scanned does, or a capture adopted
  for a folder that is no longer one. An image you chose for one is still
  found, wherever it is, so that alone is no reason to refuse.

Scanning may still show files reached through an intentionally configured
symbolic link, but Delete and Move to Trash refuse destructive authority
through symbolic-link directory components. Configure the real absolute root
when you want those actions available. This keeps a link swapped between scan
and removal from redirecting an operation outside the selected library.

Changing the list rescans immediately. Removing a folder from this list (Settings
asks first) stops scanning it; it does not delete the wallpapers in it.

## Videos and the stills behind them

A video wallpaper is played by mpvpaper, but the paired still is set through
Noctalia first. Two things follow from that: if the renderer dies the right
image is still on screen, and Noctalia's generated palette matches what the
video looks like rather than whatever was up before it.

A video finds its still in this order:

1. a `<video>.wall-in-one.json` sidecar naming one;
2. its deterministic path-keyed file under
   `<each configured root>/Wall-in-One/Automatic Stills/`, searched in root
   order;
3. a verified legacy capture recorded by the upgrade migration;
4. a sibling named `foo-still.png` -- or plain `foo.png` -- next to `foo.mp4`.

Until a video has a still, playback can't show it: the wallpaper service's
compiler leaves it out, and every page counts it the same way. Its Library
card says **No still** and its Apply is off, the playlist picker doesn't offer
it, the new window won't add it to a playlist, and Quick choice refuses it
anywhere. A playlist something uses can't keep it as its only wallpaper that
plays, and no playlist may hold only such videos: the service couldn't build
its configuration around one. Clearing or resetting a video's chosen still is
refused when it would leave a playlist either way.

Automatic captures, including verified captures from older releases, stay
internal to their moving wallpaper. A sibling or manually added image is
user-provided and remains a separate library item with its own Pairing, even
while one or more moving wallpapers use it as their representative.

New automatic captures are written under the first configured root. Existing
deterministic captures are discovered under every configured root so changing
root order does not strand an older representative.

At app or wallpaper-service startup, verified older automatic captures are
replaced with current captures, generating missing ones and rebinding the
videos. The app refreshes its playback configuration before deleting the old
images and their metadata to reclaim space. A video already bound to a separate
still keeps that choice; its unused old capture needs no rebuild. If rebuilding
or runtime handover fails, cleanup waits for a later startup; the old images are
not kept as permanent backups. Unrecognized or modified files, manual images
and captures still explicitly referenced by saved choices are left untouched.

An explicit representative choice must already be a still item in the latest
library scan. The details pane's **Use an image file instead…** and
`wall-in-one ctl still` refuse an outside file, an unindexed file, or an
indexed video. Wall-in-One does not silently import, copy, move, or later
delete an external picture. To use one, copy or move it into a configured
library folder, or add its folder under **Settings → Library**, rescan the
library (`F5`), and choose the newly indexed still. Automatic generated captures
remain internal defaults rather than separate choices in the picker.

### Stills the app makes for itself

The app tries to capture a still for each video that lacks one. It takes a
full-resolution PNG three seconds in to avoid opening fades, and records the
pairing in a sidecar when the video itself is inside the target library root.
Videos from another configured root still use the deterministic managed still;
the app does not write a pairing file beside a source outside that target root.

A Wallpaper Engine scene gets its still from `linux-wallpaperengine`, rendered
in a window at the size of a display niri measured, as `<scene id>.png` in the
first library folder's `Wall-in-One/Automatic Stills/`. A scene whose managed still is smaller than that display in both
directions is captured again, so an old low-resolution still is replaced. A
still is never judged against a guessed size or by its shape, and nothing is
written without the engine or a measured display.

Generated filenames include a digest of the video's absolute path. Two files
named `intro.mp4` in different folders therefore get different stills rather
than silently overwriting each other.

- Captures run after each rescan, including the first one when the app opens,
  on a single background worker, with no click. They are the one thing an
  idle app writes into the library (see
  [What opening the app writes](updating.md#what-opening-the-app-writes)).
- Each wallpaper is attempted once per session, so a failed capture does not
  keep retrying on every rescan.
- It needs `ffmpeg`. The Nix package puts it on the app's `PATH`. Without it,
  a video that already has a representative still (a sibling, a capture made
  earlier, or one you chose) plays as usual. One with none says "No still yet"
  and can't play until it has one: choose an image for it in its details, or
  make capture available.
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

The Library's search box sits in the header (`Ctrl+F` focuses it), and the
filter row has the kind filter, a **Favorites** chip, the count, and a menu for
the sort and the thumbnail size. All of them work over the complete scanned
inventory without rescanning the filesystem. Search, kind and sort reset
between launches, so the next launch starts with the full library visible.

The grid builds 72 cards at first, then offers **Show 72 more · 72 of 300
shown**, one page at a time. Matching and sorting still cover all 4096
supported items, and the wallpaper on screen and the one whose details are open
always get a card. This bounds GTK widget and thumbnail work while keeping the
complete library reachable. A new search, filter or sort starts again from 72.
A thumbnail is requested when its card is first built; the on-disk cache
prevents a repeat decode unless the media changed.

**Search** splits what you type on whitespace and requires every word to appear
somewhere in the wallpaper's name, in any order: the filename without its
extension, or a Wallpaper Engine item's title. `snow vil` finds
`snowy-village-still`. Matching folds accents and case, so `cafe` finds `Café`.
Folder names and extensions are not searched.

**Kind** offers All, Images, Videos and Scenes; the **Favorites** chip narrows
any of them to your favourites.

**Sort** offers Name, Newest first (modification time), and Largest first.
Ties fall back to name and then path, so the grid does not reshuffle equal-sized
files on every rescan. The same menu switches between large and small
thumbnails, which is saved in `ui.toml`.

Beside the filters the count reads `300 wallpapers`, or `3 of 300` while the
search or a filter hides some, with `· scanning…` during a scan. Sorting alone
does not narrow the results.

*In the classic interface* the search, kind and sort sit in a row under the
header, Kind offers Everything, Stills only, Videos only and Favourites only,
and the grid offers **Load more**.

## Favourites

Use the star on a Library card (it appears when you point at the card, and
stays on a favourite) or in its details to mark a favourite; **Select** stars
several at once. Favourites are saved separately from settings in
`~/.local/state/wall-in-one/favourites.json`.

Favourites are kept in the order you starred them. Each change must be saved
before the app adopts it. If saving fails, the star stays in its previous
state and an error is shown; there is no session-only favourite change.

**A favourite whose file is not in the library is kept.** Removing a root from
the scan, disconnecting a drive or reaching the scan limit does not clear its
star. Unstarring the item or explicitly removing its wallpaper clears it.

**Settings → Playback → Cycle favourites only** narrows the rotation to them.
If no favourites are available to play, rotation falls back to the whole
library.

## Using or taking away an item

Clicking a Library card opens its details beside the grid; it never applies
the wallpaper. On a focused card, Enter does the same and `Ctrl+Enter` applies
it. The details pane has:

- **Apply**, to the displays the player bar is set to, or from its arrow to all
  displays or to one display only (offered while displays run separately). It
  plays the wallpaper through the hidden one-entry **Your pick** playlist: one
  for all displays, or one per display.
- the favourite star;
- **Still**, for a video or a scene: **Use an image file instead…**, **Use the
  automatic still**, or, for a scene, **Capture a new still**;
- **Colors**: **From wallpaper**, with the Settings scheme (Default) or one of
  Noctalia's ten schemes; a **Palette**; or **Don't change**. Then **Light or
  dark** and **Reset to defaults**;
- **In playlists**, with **Add…**;
- **File**: where it is, **Show in Files**, where it came from, and how it can
  be removed.

A card's menu (right-click, the ⋯ button, the Menu key or `Shift+F10`) has
Apply to all displays or one, Edit details, Add to playlist, the favourite,
Reset to defaults, Show in Files, and the removal. Opening the menu does not
apply the wallpaper; entries which do not apply to that item are omitted.
**Select** picks several cards to add to a playlist or favourite together.

The palette choices list Noctalia's built-ins, your palettes, and the
community palettes 24 at a time. Built-ins have no colour preview because
Noctalia does not expose their colours without applying them.

*In the classic interface* clicking a tile opens its full-size pairing editor
instead, and Apply is a button on the tile.

The removal is **Delete from disk…** or **Move to Trash**, in the card menu, the
details pane and the selection bar, one wallpaper at a time.

- **Delete from disk…** appears for a downloaded file whose app ownership can
  be verified. It permanently deletes the wallpaper and its verified app-owned
  sidecars and generated stills. It asks for confirmation and cannot be undone
  through Trash.
- **Move to Trash** appears for your own files inside a configured library
  root. It moves them to the freedesktop home trash under
  `~/.local/share/Trash`, where a file manager can restore the media. It does not
  ask for confirmation. Restoring the file does not restore the app metadata
  cleared by removal, such as favourites and playlist occurrences.
- Neither is offered for a Steam Workshop item (unsubscribe in Steam) or a file
  outside your library folders, and neither removes the last wallpaper that
  plays from a playlist something uses. That includes an image another video
  plays through, as the still you chose for it or its sibling, when the video
  would have no other: removing the image takes its still away, under any path
  it is reached by. So does removing a video whose capture another video was
  given. Removing has no Undo in the app.

*In the classic interface* the same two entries are named **Remove** and
**Move to Trash**.

### How a change is checked

A change that can take away what a playlist plays (Delete from disk, Move to
Trash, removing a library folder, turning off Wallpaper Engine scanning) is
first made for pretend, after you ask and before anything is moved, deleted or
saved, on the same worker that then makes it:

- the real scan of the library as the change would leave it. A file the
  removal deletes (the wallpaper, its sidecars and the stills that go with
  it, including a still being captured for it that isn't there yet) counts as
  gone under every path that reaches it: through a linked folder, a bind
  mount, or a symbolic link to it, and wherever else the scan would look,
  such as Wallpaper Engine's folders. Another hard link to it stays, as it
  does on disk. While it is tried, no still for that wallpaper can be saved;
- the removal's own cleanup of favourites, playlist entries and still choices,
  on copies;
- the build of the runtime configuration from what is left.

The change is refused when a playlist something uses would have nothing left
to play, or any playlist would hold only wallpapers playback can't use. The
refusal names the playlist, the entry and the still, for example: *forest was
not removed: “Alias” (used by 1 schedule rule) would have nothing left to
play: rain.mp4 plays through forest.png, which this removes.* Nothing is
written while it is tried.

What pages show as you go is early feedback from what the app already knows:
a card's **No still**, a wallpaper the playlist picker doesn't offer, a
removal refused as you choose it. The check above is the guarantee.

The check only decides whether the change is made. After a removal, the
library is scanned again, on the same worker, and only what that scan finds is
shown and published. Until a scan after the removal has been accepted, the
library the app holds may still name the removed file, so nothing new is
installed from it, whatever asks: an edit is saved, and reaches the wallpaper
service with the next publication after that scan. Meanwhile the service keeps
the last setup that was installed, which can name the removed file until it
is replaced. So no runtime configuration installed after one of the app's own
removals names the file it removed. A removal that went through with a
problem (a cleanup that failed) is scanned once more after that, with the
cleanup tried again.

That guarantee covers what the app itself removes. A file deleted outside the
app (in a file manager, say) after the last accepted scan is different, as it
was in 0.2.2: the library the app holds still names it, so a later ordinary
publication, after an unrelated edit, can still name the missing file until
the next scan. If that publication is refused, the notice under the header
says why (the classic window says it in a toast) and the service keeps its
last setup. The next scan (`F5`, or **Rescan the Library** in the main menu)
drops the file, and the publication after it names only what is there.

If the scan after a removal fails, or the runtime configuration can't be
written or is refused, the notice under the header says so and the service
keeps its last setup; nothing waits on it, so the app can still quit, also
when its window was closed meanwhile. Refreshing the library, or the next
change that gets through, recovers. Another removal that doesn't go through
changes nothing: in particular, it doesn't make a library that still needs
that scan count as scanned.

The check costs about one scan and one build of the runtime configuration, and
a removal one more scan after it, all on the worker rather than in the window.
One measurement on one machine, not a bound: with about 2,100 wallpapers, all
in one playlist, the check took about half a second and the whole removal,
scan after it included, about 0.7 seconds; the same run on a busy machine
took several times as long.

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

So before anything is moved or deleted, Remove and Move to Trash read
`favourites.json`, `pairings.json` and `playlists.json` again. If any of them
was saved by a newer version of Wall-in-One, or cannot be read, the whole
removal is refused: the message names the file, and the wallpaper is left
where it is. Records that still cannot be cleared after the media commit, for
example because a newer version saved a file in between, are reported as
incomplete cleanup with a refresh to retry, never as nothing having changed.

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

A playlist's page adds wallpapers with **Add wallpapers…**: a picker with its
own search and kind filter, where the wallpapers you click are numbered in the
order they will go in, then **Add N** (or `Ctrl+Enter`) adds them in one save.
You can also drag a Library card onto the playlist's rows, where it lands
where you drop it (below the last row shown, at the end), or onto its row in
the sidebar, or use **Add to playlist** in a card's menu or the details pane.
Drag a playlist row's handle to reorder it, or focus the row and press
`Ctrl+Up` / `Ctrl+Down` (`Ctrl+Home` / `Ctrl+End` for the top and bottom); the
row's menu has the same moves. Reordering preserves each occurrence's stable
entry ID, so the same wallpaper can appear more than once (the picker marks one
already there) without those occurrences becoming interchangeable. Removing
entries, with the row's − button, `Delete`, or **Select** for several, shows a
toast with **Undo**.

The picker doesn't offer a wallpaper that playback skips, nor a video whose
still isn't there yet, since that can't play either (see
[Videos and the stills behind them](#videos-and-the-stills-behind-them));
hovering one says why, and adding one any other way in the new window is
refused with the reason. An older playlist occurrence is retained and
labelled (`Missing · <file>` or `Playback unavailable · <name>`) so it can be
removed deliberately; playback skips it when another usable occurrence exists,
and **Play now** is off when every entry is missing or skipped.

A playlist's files can still go away outside the app, or with a drive. Its
entries are kept, and when nothing in a playlist can play any more, the
wallpaper service keeps the last setup that worked. The app names the
playlist, and the new interface keeps saying so under its header, with a
button to Playlists, until a change gets through again. An older profile's
default, schedule rule or display assignment that names a playlist by its
name, rather than its id, keeps working: it is given the playlist's id at the
next start, and a rename or delete takes it along.

Playlists can contain up to 10,000 entries. The page initially builds 72 rows
and offers **Show N more**; loaded rows keep their identity for keyboard and
drag reordering. The stored order is complete throughout: paging limits
interface work, not the runtime playlist or the entries written to disk.

*In the classic interface* the Playlists tab adds wallpapers from a searchable
Library list with **+**, double-click, Enter or drag-and-drop, and its order
pane offers **Load more**.

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

Taking something out is not a repair. Deleting, unassigning, unstarring,
forgetting or renaming a record in one of these files while it can't be read
is refused with the reason `unreadable` ("… can't be read (…), so which rules
it holds isn't known. Nothing was changed."), and the file stays where and as it
is: nothing can be said to be gone from a file whose contents aren't known.
Renaming a playlist reads every file that names it before it writes any of
them, so it is refused the same way if one of them can't be read.

Each of these files has a size the app reads back: 8 MiB for playlists and
Pairings, 4 MiB for schedules and favourites, 64 KiB for display assignments,
and 1 MiB for `settings.toml`. An edit that would make one larger is refused
before anything is written ("… would be N bytes, more than the M bytes this
version can read back; remove some playlist entries first. Nothing was
changed."), so the file stays readable as it was. For the library files the
refusal's reason is `full`; for `settings.toml` it is a settings error with the
same explanation.

From 0.2.0, a file saved by a newer version of Wall-in-One is not unreadable,
and it is never moved aside or rewritten. The app shows what it understands of
it and says once which files are affected; every edit to such a file is refused
with the reason `newer-version` ("… was saved by a newer version of Wall-in-One;
open that version to change it. Nothing was changed."), and the runtime keeps
its last-known-good configuration. The pending-removals journal is never acted
on at all in that case, and while favourites, pairings or playlists is such a
file, Remove and Move to Trash are refused before the wallpaper is touched.

Fields the app does not know, for example ones a newer version added, are kept
through every save of a file it does understand: at the top level and on each
playlist, playlist entry, schedule rule, pairing and removal intent.

0.1.4 has neither guard. It reads a newer file as unreadable and drops fields
it does not know. Its app checks the playlists, schedules and display
assignments before it saves anything, so while one of them is from 0.2.0 it
pauses every edit instead, and nothing is moved aside or lost. To go back to
0.1.4, `wall-in-one-rollback` rewrites those files in 0.1.4's versions first;
see [Going back to 0.1.4](updating.md#going-back-to-014).

### Format versions and their backups

A file moves to a newer format only when you first use what that format adds.
Every other edit keeps writing the version the file already has, so a profile
that never uses a new feature stays readable by older releases. The first rule
name is the first such change: `schedules.json` stays at version 2, exactly as
0.1.4 writes it, until a rule has a name, and is version 3 from then on.
Likewise `playlists.json` stays at version 1 until a playlist has its own
interval or shuffle, and `displays.json` until a display's own playlist is set
to beat global schedule rules; each is version 2 from then on. Those settings
reach the service through `runtime-overrides.toml`, never `runtime.toml` (see
[the runtime contract](runtime-config.md#per-playlist-rotation-and-display-precedence-runtime-overridestoml)).

Just before a file is first saved in a newer format, its previous bytes are
copied beside it as `<file>.v<old>-backup`, for example
`schedules.json.v2-backup`, with the same permissions. That copy is made once
and never overwritten, even if the file is bumped again later; the app never
deletes it. If the copy cannot be made, the edit is refused with the reason
`no-backup` and nothing is changed. A file stays in its newer format after
you stop using the feature, for example after clearing the last rule name, so
it does not switch formats back and forth and the backup always holds the
file as it was before its first bump.

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
| `<affected parent>/.wall-in-one-retained/entry-*` | private exact cleanup residue; zero tombstones and intact retired transaction records/candidates are expected (a transient claim container is removed once its discard empties it; an empty one may remain from 0.2.2 or earlier, or after an interrupted discard), while multiply linked files, sockets and unexpected evidence remain intact. Settings → Tidy up archives only the provably inert ones: empty claim containers untouched for an hour and empty, singly linked records |
| `~/.config/wall-in-one/tidy-archive/<date>-<action>/` | what Settings → Tidy up moved or backed up, with `manifest.json` (where each item came from) and a README; Undo uses it. Nothing in it is ever deleted |
