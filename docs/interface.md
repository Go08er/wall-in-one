# The window, page by page

Wall-in-One opens the new interface: a sidebar of pages beside one content
area, and a player bar along the bottom. This page describes what each part
does. The README's [Using it](../README.md#using-it) is the short version, and
says how to choose the classic window instead.

## The window

- **The sidebar** lists Library and Store; your playlists, then **All
  wallpapers** and **New playlist**; Schedule and Displays under Automation;
  and Settings. `Alt+1` to `Alt+6` reach the pages in that order.
- **The main menu** has **Rescan the Library** (also `F5`), the window style
  (Solid, Translucent or Frosted) with **Opacity and frost…**, **Keyboard
  shortcuts** and **About Wall-in-One**.
- **Notices** under the header say what the window is waiting for, such as a
  first-run question put off or files saved by a newer version, and the
  wallpaper service's state, such as "The wallpaper service isn't running".
  A renderer that stopped, degraded display discovery or an invalid status
  reply is said once, in a toast.
- **How it saves.** Each change goes through the save classic uses for it, one
  at a time on the app's ordered authoring lane. Each page shows what was
  saved: a change that fails is reported, and the page goes back to what is
  saved. A refusal says why.

## First run

The new interface asks classic's first-run questions, in the same words, on a
welcome screen instead of in dialogs:

- whether to import settings and playlists from the older Noctalia plugin
  (Import safely, Start fresh, Not now), and Try again if that check fails;
- the library folder: the exact folder it found, Use default, or a folder
  chooser.

"Not now" saves nothing and the question comes back at the next start. Until
then a notice under the header says what is waiting, and its button asks
again. While the import question is open, the library isn't loaded and
changes can't be saved, and the notice says so.

When changes stop reaching playback (a playlist's files went away outside the
app, say), the same notice says why, naming the playlist, with a button to
Playlists; the wallpaper service keeps the last setup that worked until a
change gets through again.

## After an update

Classic's one-time Tidy up card is above every page; Review opens Settings →
Tidy up. Nothing there runs until you press its button, including the fix for
Noctalia's old palette template name.

## The player bar

- **What it says:** what the runtime reports and why ("Evening · from
  schedule", "Your pick").
- **Live controls:** play/pause, next, previous, shuffle and "Change wallpaper
  automatically"; its ⋯ menu adds Resume schedule, random, Stop and "Why this
  wallpaper?". They are the classic window's runtime commands, sent the same
  way. `Ctrl+Space` is play/pause.
- Every playback control acts on the displays the player bar is scoped to,
  all of them or one. Mirrored displays, or a display that has gone away, get
  the command for all of them.
- **Apply** goes to all displays, or to the display the bar is scoped to.
  - A "<display> only" chosen from a card's or the details pane's Apply menu
    is never widened like that. If that display has gone, or the displays
    are mirrored, nothing is sent and a notice says so.
  - Apply, favorites and playback have no undo yet.
- **The playback controls** are off until the wallpaper service has answered,
  and while it isn't running; the app doesn't start the service.

## Library

A paged grid with the details pane beside it.

- **Finding one:** the search box in the header matches names; the kind
  filter, the Favorites chip and the sort sit above the grid, with the count,
  which also says how many files the scan left out. See [Finding
  one](library.md#finding-one).
- **A card:** click it, or press Enter, to open its details; `Ctrl+Enter`
  applies it. Its star marks a favourite, and its menu (right-click, ⋯, the
  Menu key or `Shift+F10`) has Apply to all displays or one, Edit details, Add
  to playlist, the favourite, Reset to defaults, Show in Files and the
  removal.
- **The details pane** changes what classic's tile menu and pairing editor
  change, through the same saves:
  - **Colours:** From wallpaper, with the Settings scheme (Default) or one of
    Noctalia's ten schemes, each previewed from its still; a palette; or Don't
    change. Also **Light or dark**. A colour choice shows while it is being
    saved.
  - **Still**, for a moving wallpaper: a library image instead, the automatic
    one again, or a new capture of a scene.
  - **Reset to defaults.**
  - **Try again**, for a wallpaper playback skips. Its Apply is off, with the
    reason, until then.
  - A video waiting for its still says **No still** on its card, and its
    Apply is off with the reason until it has one.
- **Add to playlist**, from:
  - a card or the details pane;
  - a drag to a playlist in the sidebar or onto the Playlists page;
  - several selected at once (**Select**).

  It adds to a playlist, or to a new one made with them in it. It is the
  Playlists page's own save: one save, never a wallpaper playback can't show,
  never "Your pick". Selecting several also favorites them together.
- **Remove** is named for what it does:
  - a download Wall-in-One made is deleted after a question (**Delete from
    disk…**);
  - your own file goes to the Trash (**Move to Trash**);
  - a Workshop scene, or a file outside the library folders, is not
    Wall-in-One's to remove.

  It removes one wallpaper at a time, and never the last one in a playlist
  something uses. Removing has no Undo, because its favorite, colours and
  playlist entries go with the file.
- **Add a folder**, the **+** in the header, uses Settings' own folder path and
  scans once the folder is saved.
- **Keys:** F5 rescans; Ctrl+Shift+R, Ctrl+B, Ctrl+P and Ctrl+W work as in
  classic.
- Not there yet: per-wallpaper motion and "Use frame".

## Store

It searches Wallhaven and MotionBGS through the classic Store's code, and opens
on what is popular once you first show it.

- **Filters** sit inline under the provider row (never in a popover):
  - sort, and "Top list covers" for the top list;
  - minimum size and aspect ratio;
  - categories and content rating;
  - Wallhaven's 29 search colours.

  MotionBGS browses latest, 4K, HD or a genre, and its quality picks the file
  a download takes.
- Results come 40 to a page with Previous and Next.
- **Downloads** go into your first library folder, one at a time, with
  "Downloading 2 of 5" and no pretend progress or cancel.
  - "Download & apply" and "Show in Library" wait for the library rescan that
    follows.
  - A download that landed in a folder Settings replaced meanwhile is said so
    and not applied.
  - With no library folder, Download is off with the reason, and a press that
    reaches it anyway says to add one in Settings → Library, with a button
    that goes there.
- **A search that fails** shows why in the site's words, with Try again, or
  Reset filters when the filters made it impossible.
- **NSFW results** need a Wallhaven API key. The Store says so in Wallhaven's
  words and links to the Settings page's Wallhaven key, where it is saved.
- **Keys:** Space picks a result, Ctrl+A picks the page and Ctrl+D downloads
  the picks.
- **Steam Workshop:** the ⋯ menu hands off to it when Steam is installed.

[Searching and downloading wallpapers](browsing.md) has the details.

## Playlists

The page edits your own playlists:

- create, rename, duplicate and delete them. Delete names the schedule rules,
  display assignments and default that go with it.
- add several wallpapers at once from its picker, or drop one from the Library
  where it should go;
- reorder by a row's handle or Ctrl+↑/↓; moves apply in order, each from where
  the entry is when it is saved;
- remove entries, with Undo.

Also:

- A playlist can have its own interval and shuffle, or follow the defaults.
- Play now and Use as schedule default follow the classic page's rules.
- A wallpaper is counted as one that plays by the same rule the wallpaper
  service compiles with: a video waiting for its still doesn't play yet, so it
  can't be added. The picker doesn't offer it and says why, and adding it any
  other way is refused, saying why.
- A playlist that a schedule rule, the default or a display uses can't lose
  its last wallpaper that plays.
- No playlist, used or not, can be left holding only wallpapers playback can't
  use: the service couldn't build its configuration around it. It can be
  emptied.
- One with nothing that plays can't become the default.
- "Your pick" stays hidden.
- Long playlists show 72 rows at a time. A wallpaper dropped below the last row
  shown goes to the end of the playlist.

See [playlist behavior and limits](library.md#playlist-behavior-and-limits).

## Schedule

A week calendar and the rules, highest priority on top. That is the stored
order reversed, where the last matching rule wins.

- **Adding:** drag on the calendar, or use Add rule.
- **Priority:** drag a rule by its handle, or use Ctrl+↑/↓.
- **The rule menu** edits, duplicates or moves a rule, and deletes it after
  asking.
- **"When nothing is scheduled"** is the default playlist.
- The "Now" card and its "Why?" are the runtime's answer: its rule and its
  "until". The calendar only projects the week.
- **Mirrored displays:** a rule aimed at one display is kept but shown inactive
  while they are mirrored.
- **Refused:** a time window that starts and ends on the same minute, or a
  playlist with nothing to play, can't be saved.

## Displays

- **The mode:** "Same on all displays", or "Each display separately". That
  needs a display for the desktop colors: the one saved, even while it is
  disconnected, or else the first connected one. With neither, it is refused.
- **The compositor's layout.**
- **For each display:**
  - what it shows and why, and Play now;
  - its Cycle and Shuffle, with "Use saved defaults". These are runtime
    choices, never saved.
  - its own playlist, for when nothing is scheduled;
  - whether that playlist beats the shared rules. This is kept in
    `runtime-overrides.toml`, never in `runtime.toml`.
- **Which display the desktop colors follow.** It and the mode are the same
  settings, saved the same way, as Settings → Displays.
- **A display that is not connected** keeps what is saved for it until you
  forget it.

## Settings

Every setting classic Settings has, each saved to the same `settings.toml` key
the same way, in searchable groups:

- library folders: add, remove, and make one the folder downloads and generated
  stills go to. A folder found on this machine is only suggested. One whose
  wallpapers a playlist needs can't be removed: Settings asks as it does for
  any folder, then the change is tried for pretend before it is saved and
  refused, naming the playlist and what it would lose; turning off Wallpaper
  Engine scanning is refused the same way (see
  [How a change is checked](library.md#how-a-change-is-checked)).
- playback defaults;
- displays, and which one the colours follow;
- the app's colours and default scheme, the palette manager, and Noctalia's
  template;
- the window's look, the Interface and GPU acceleration rows (so you can go
  back to classic from inside the new interface), and, in its own **Classic
  interface** group, the classic window's opacity, which this window doesn't
  use;
- the Wallhaven key;
- video and Wallpaper Engine renderer settings;
- Tidy up;
- a log of what Wall-in-One and the wallpaper service reported since the app
  started, kept in memory only, with **Copy** and **Hide file paths** (on at
  every start, never saved). Classic has no log. Hide file paths replaces your
  home folder, your library folders, the app's own folders and your login
  name. It is not anonymization: file names stay readable, and so does a path
  outside those folders (apart from your login name in it).

A save that fails is reported, and every row goes back to what is saved.

**Palettes** opens the palette manager:

- browse every installed palette and apply one through Noctalia;
- duplicate one into yours, named "<name> copy" (numbered if that is taken),
  and edit its colours;
- preview each scheme on a still, then use it as the default or keep it as a
  palette.

Like classic, it has no delete. A new palette's name is checked as you type:
Save waits, with the reason, until no other palette has it.

**Noctalia template** says whether Noctalia renders your colours for
Wall-in-One, and installs or repairs that when you press its button.
Installing doesn't render; **Render now**, on the row **Render the colours
now** and offered until Noctalia has, asks it to re-render all of its
templates.

## When it can't change things

- **A store saved by a newer version** turns Apply, favorites, library changes
  and the playback controls off and says so in a notice under the header; the
  Playlists page says so too and changes nothing, and so do the Schedule and
  Displays pages.
- Settings keys this version doesn't know are named in that notice as
  read-only, and the Settings rows that write `settings.toml` are off.

## Its own preferences

These live in `ui.toml` and are written only when you change one of them:

- the window style: solid, translucent or frosted;
- its opacity and frost dials, in the main menu and Settings → Appearance;
- the thumbnail size, in the Library's sort menu.
