# Migration and upgrade guide

Wall-in-One has two separate compatibility boundaries. The deployed
Python/Rust application upgrade below is automatic only for one exact shipped
schema-2 profile. The retired Noctalia Luau plugin import is an explicit user
decision for a different format. Their commands, journals, and authority
records are not interchangeable.

## Upgrading the deployed Python/Rust app

One deployed Python/Rust release used `roots = []` to mean “follow Noctalia's
wallpaper directory” and compiled a schema-2 `runtime.toml`. The current app
requires an explicit library root and the Rust service requires runtime schema
4. The deployed-upgrade boundary recognizes that one transition; it is not a
promise to migrate arbitrary pre-release application state.

### Detection and fail-closed behavior

Graphical startup and unattended writers such as `--write-config` first run
the deployed-profile detector. It proceeds automatically only when all of the
old profile's independent evidence agrees: the exact predecessor-shaped
`settings.toml` with `roots = []`, a valid schema-2 runtime, the current
authoring stores, Noctalia's absolute wallpaper directory, the exact owned
Automatic Stills marker, and the runtime's source/capture relationships. Files,
directories, source generations, captures, and any relevant sidecars are
bounded and revalidated without following symbolic links. Playlist, schedule,
display, All Media, and authoring-store contents must describe the same
snapshot.

The detector then has only these outcomes:

| Status | Meaning |
|---|---|
| `absent` | No settings or orphaned app state exists. This is a fresh install. |
| `current` | Settings already use the explicit-root contract; no deployed upgrade is needed. |
| `ready` | One exact schema-2 predecessor is eligible for automatic preparation and cutover. |
| `in-progress` | An exact transaction journal exists and can be resumed. |
| `prepared` | Schema 4 and capture authority are staged; resume verifies whether schema 2 or an interrupted schema-4 cutover is public. |
| `complete` | The schema-4 cutover and completion record agree. |
| `conflict` or `corrupt` | Evidence is ambiguous, inconsistent, unsafe, or malformed; nothing is guessed. |

A genuinely fresh install remains outside this migration. The initial probe
runs before creating a persistent transaction lock, so it does not create or
modify the app's XDG config/state trees; the GUI continues to ask the user for
a library directory. State files without settings are not treated as fresh:
they are reported as corrupt so defaults cannot overwrite an incomplete
profile.

Examples which fail closed include a changed source or capture, duplicate
source/capture claims, a missing or altered marker, a basename that does not
match its video, a pre-existing adoption sidecar without the journal that
created it, a current hashed capture beside the predecessor capture, an active
removal intent, mismatched runtime/authoring data, a hard link, a symlinked
ancestor, or a path that cannot be pinned beneath its expected root. Re-running
the command does not turn any of those cases into authority.

### Inspect, prepare, and cut over

The status command is read-only. Preparation is intentionally distinct from
the final service cutover:

```console
$ wall-in-one --deployed-upgrade-status
$ wall-in-one --prepare-deployed-upgrade
```

`--prepare-deployed-upgrade` refuses to run while a Python authoring process is
listening on the app socket. After taking the profile/compiler/authoring locks,
it writes the durable journal first, publishes exact capture-adoption
sidecars, replaces the old empty-root setting with the proven original root,
publishes the central capture-authority manifest, and compiles and validates a
private schema-4 stage. The public `runtime.toml` remains the byte-exact
schema-2 generation, so preparation leaves an already-running old Rust
daemon's public input untouched. If a crash already committed the schema-4
runtime, preparation resumes forward and records completion instead of
pretending it can return to an earlier boundary.

Normal graphical startup and `wall-in-one --write-config` run the same
transaction with cutover enabled. Immediately before cutover, the app
revalidates the journal, settings, authoring stores, capture authority, and a
deterministic re-render of the stage. It then atomically claims only the exact
schema-2 runtime generation, installs the validated schema-4 bytes, verifies
them again, and writes the completion marker last. Repeated preparation,
startup, or compilation resumes the same journal and is idempotent; it never
starts a second migration from newly discovered pathnames.

The reserved state records are
`.deployed-upgrade-v1.journal.json`,
`.runtime.toml.deployed-upgrade-v1.stage`,
`deployed-capture-adoption-v1.json`, and the final
`deployed-upgrade-v1.json`, all below the app state directory. Their presence
is parsed and cross-checked; a file merely occupying one of those names is not
accepted as progress or completion.

The deployed-upgrade commands use sysexits-style failure codes:

- exit `75` (`EX_TEMPFAIL`) means retryable exclusion failure, such as a live
  Python writer, an uncertain authoring socket, or a busy migration lock. Stop
  the old process or let the other transaction finish, then retry.
- exit `78` (`EX_CONFIG`) means the persisted evidence is conflicting,
  corrupt, or otherwise unsafe to migrate automatically. Preserve the files
  and inspect the reported path; repeated retries without resolving the
  evidence will not help. `--deployed-upgrade-status` returns 78 for
  `conflict`/`corrupt` and zero for its other read-only classifications.

The packaged service treats an incompatible/malformed runtime as a
non-restartable exit-78 configuration boundary. A failed cross-schema startup
therefore stops with a diagnostic instead of exhausting the restart burst
against a surviving schema-2 document.

### Root and capture preservation

The transaction makes the exact Noctalia directory already used by the
deployed app the explicit current root. It does not select a replacement root.
Existing full-resolution video captures remain at their original direct-child
paths under `Wall-in-One/Automatic Stills/<video-basename>.png`; they are not
renamed to the new hashed convention. Canonical `schema: 2` authority sidecars
and a central manifest bind each exact source generation to its exact capture
generation, content hash, root identity, marker, and publication. The current
scanner can therefore keep that exact capture attached to its video and hidden
as a generated child, while a replacement generation loses authority and is
shown as a user-owned file.

Migration reads and hashes each bounded capture but does not decode any video
or regenerate a frame. It never writes into a Steam Workshop source directory
or changes a Workshop media file; the new sidecars are installed beside the
captures in the proven Automatic Stills directory. Existing pairings,
playlists, schedules, displays, favourites, provider sidecars, palettes, scene
stills, and the original basename captures remain in place. Later explicit
Remove/Move to Trash cleanup may consume an adopted capture only through its
exact live generation authority; an observed Workshop uninstall remains
metadata-only.

### Crash recovery, backups, and rollback

Together, the journal, authority document, deterministic stage, and completion
record bind the original and target settings/runtime evidence, root and
directory identities, authoring snapshot, marker, captures, and operation
tokens. Publication is no-overwrite. If power is lost after an old settings
or runtime file is moved out of its public name, the exact predecessor remains
in a private durable claim and the next run recovers or advances only that
journaled generation.

Empty or unrelated claim directories are never deleted, trusted, or reused.
After a replacement is durable, predecessor claims deliberately remain inert:
they are crash evidence, not user-facing backups, and should not be renamed or
cleaned by hand. Exact journal/stage files are retired only after the completion
marker is durable; harmless exact residue is cross-checked on later starts.

Before installing the new package, stop Wall-in-One and back up:

- `~/.config/wall-in-one` and `~/.local/state/wall-in-one`;
- the selected root's `Wall-in-One/Automatic Stills` directory, including its
  ownership marker and captures; and
- the Noctalia settings and companion revision used by the old installation.

For rollback, stop and disable the new service first, preserve the failed/new
state for diagnosis, restore those backups as a unit, and restore the prior
app and companion revisions. Do not combine individual files from before and
after cutover, delete a resumable journal, or treat a private claim as the
rollback interface. There is no automatic schema-4-to-schema-2 conversion.

Release order is app first, companion second. Publish and make the compatible
app available, then immediately publish/promote the reviewed companion. For a
live machine, install the app, inspect or prepare the deployed upgrade, let GUI
startup or `--write-config` complete schema-4 cutover, and only then
refresh/materialize the strict companion and restart the service. Publishing
or loading the strict companion first can strand users whose public app still
emits the older status contract.

## Importing the retired Noctalia Luau plugin

Wall-in-One can import the retired full application from
`Go08er/goober-noctalia-plugins-v5`, plugin id `goober/wall-in-one`. This is a
one-way, explicit import into an otherwise empty current profile. It is not a
merge and it never edits, moves, or deletes the predecessor's files.

This importer is for legacy `config.json` schemas 1 through 5. It does not
implement the deployed Python/Rust schema-2 upgrade described above.

### What the first launch asks

The graphical app checks for legacy authoring before it asks for a new library
folder. When it finds data, it shows the source path, schema, playlist count,
display count, and any current files which prevent an import.

- **Import safely** is available only when the current profile is empty, or
  when an interrupted import can be resumed from its exact journal and source
  digest. It installs each generated file without replacement and writes the
  imported marker last. An interrupted journal is bound to its original
  absolute source path; retrying with a different source is refused rather
  than silently switching snapshots.
- **Start fresh** records that decision for the exact legacy source path,
  config bytes, schema, and migration-marker version. It does not delete the
  old data. If that path or `config.json` later changes, the decision becomes a
  conflict and the app asks again rather than applying it to different data.
- **Keep current** is the same durable decision when current authoring already
  exists. No automatic merge is attempted.
- **Not now** writes nothing. The migration question returns on the next
  graphical launch, and the empty-library question is not allowed to create a
  competing fresh profile during that launch.

An import which has reached its journal cannot be converted into **Start
fresh**. Some current targets may already have been installed at that point,
so the only automatic operation is to resume the exact transaction. If its
source or an installed target changed, back up both profiles and recover the
journal conflict manually; the importer will not delete either side to guess
which one should win.

Headless startup fails closed while any of those decisions is unresolved. This
prevents a login-time `--write-config` from publishing empty defaults before a
person has seen the prompt. The packaged user unit prefixes that compilation
with `-`, so an already valid last-known-good `runtime.toml` can still start;
the guard does not erase it.

The same boundary is available without GTK:

```console
$ wall-in-one --legacy-migration-status
$ wall-in-one --migrate-legacy
```

`--migrate-legacy` exits unsuccessfully on an existing current file, malformed
or future legacy schema, changed interrupted source, unsafe path, size/count
limit, or any conversion it cannot express without guessing.

### Paths and lineage

With the normal XDG defaults, the predecessor stored:

```text
~/.local/state/noctalia/plugins/data/goober/wall-in-one/config.json
~/.local/state/noctalia/plugins/data/goober/wall-in-one/runtime.json
~/.local/state/noctalia/settings.toml
```

The last path contributes only
`[plugin_settings."goober/wall-in-one"]`. The current application instead
uses:

```text
~/.config/wall-in-one/settings.toml
~/.config/wall-in-one/wallhaven-api-key
~/.local/state/wall-in-one/pairings.json
~/.local/state/wall-in-one/playlists.json
~/.local/state/wall-in-one/schedules.json
~/.local/state/wall-in-one/displays.json
~/.local/state/wall-in-one/favourites.json
~/.local/state/wall-in-one/pending-removals.json
~/.local/state/wall-in-one/runtime.toml
~/.local/state/wall-in-one/legacy-migration-v1.json
~/.local/state/wall-in-one/legacy-migration-v1.md
```

The imported marker and human-readable report are app state, not permission to
modify the old plugin directory. A private cross-process lock covers the whole
probe/check/stage/journal/install/cleanup transaction, so two login/manual
imports cannot combine different legacy snapshots.

The imported marker is a strict completion record, not just a two-field flag.
It records the normalized source, supported schema, source digest, UTC completion time,
report path, and every installed target's absolute path and historical digest.
All required target identities must still be bounded regular files when the
marker is read. Their bytes may change through normal authoring after import;
the historical hashes are not permanent content pins. The imported Wallhaven
key is the sole optional-presence target because clearing a credential
normally deletes that file. Missing fields, duplicate JSON keys, boolean or
unsupported versions, malformed hashes/timestamps, unexpected target names,
changed paths, symlinks, oversized files, a missing required target, or a
marker/journal mismatch all become an explicit conflict.

The declined marker is separately strict and binds the decision to the exact
normalized source and `config.json` digest; its bounded schema note may retain
an unsupported future value without making that schema importable. A source directory which is itself
a symbolic link is not followed, and its device/inode identity is rechecked
across a read so one import cannot combine documents from two directory
generations. An old migration report already occupying the new report path is
also an initial no-overwrite conflict.

### What is preserved

The importer preserves, subject to current typed limits:

- schema-2 per-output reels and schema-3-through-5 named playlists, including
  stable ordering, duplicate occurrences, Quick Choice collections as ordinary
  named playlists, and absolute sources which are currently unmounted;
- shared still and palette pairing metadata, including manually selected
  stills as associations rather than owned files;
- automatic still associations only when an exact adjacent predecessor
  ownership sidecar proves the dynamic id and path;
- schedule order, enablement, playlists, months, weekdays, local time windows,
  and last-match-wins behavior; schema-3 rules with no months become all-month
  rules, and a wrapped after-midnight tail remains owned by the start day;
- fallback playlist assignments and independent-display mode when more than
  one legacy output exists;
- capture/video library roots, palette generator, cycle setting, the
  designated display, and the designated display's global renderer settings;
- the Wallhaven API key, when valid, into a private mode-0600 app-owned file;
- installed Wallpaper Engine video identity as its actual video path and true
  scene identity as its stable Workshop id; and
- legacy Wallhaven/MotionBGS directory markers and per-file sidecars already on
  disk. Predecessor marker shapes remain recognized and the sidecars are
  preserved as historical metadata, but an unbound predecessor sidecar cannot
  prove the current media lifecycle. That media remains playable, deliberately
  scans as user-owned, and uses Move to Trash rather than irreversible Remove.

Independent-display import targets a software-supported code path. Routing,
schedules, cursors, renderers and transport are covered by automated tests, but
neither the development machine nor the desktop VM exposes two outputs. The
result has not been validated with two physical monitors showing different
playlists.

Missing file-backed media and manually selected stills keep their absolute
paths and are called out in the report. No replacement file is invented.
Unknown/uninstalled Workshop ids keep the expected Steam path with a warning;
reinstall or rebind them before playback.

Explicitly using local **Remove** or **Move to Trash** on an imported moving
item later removes its pairing metadata and every provably generated automatic
artifact found under the configured roots. A user-selected still remains its
own library item; deleting the association never deletes that still. An
externally observed Steam Workshop uninstall clears authored metadata only; it
does not rediscover or delete unpinned generated files.

### Intentional non-equivalences

The current authoring model is not a byte-for-byte continuation of the Luau
model. The import report records the choices which apply to the detected
profile:

- per-playlist order and interval become one global cadence, chosen from the
  designated display's fallback playlist;
- per-output renderer controls become global settings chosen from the
  designated display. Other displays' mute, hardware decode, pause, FPS,
  scaling, clamp, renderer-enable, and related variants remain only in the
  untouched legacy config;
- legacy scene scaling accepts `default`, `stretch`, `fit`, or `fill`.
  `default` maps to the current empty value (omit the flag); the other values
  map directly. Clamp accepts and maps `clamp`, `border`, or `repeat`. The
  legacy defaults `fill`/`border` therefore remain explicit after import;
- renderer layer, arbitrary mpv options, Workshop volume/silent choices, and
  the seven legacy Workshop flags have no current persisted setting. Nondefault
  values are reported and not applied;
- the old `FULL`/`MAX`/`ACTIVE` mpv auto-pause modes collapse to the current
  play-or-pause-when-hidden setting;
- mouse gesture bindings remain a companion-plugin/shell concern;
- runtime cursors, shuffle bags, history, pause state, transient manual/Quick
  Choice pins, diagnostics, last captures, and owned child PIDs reset;
- there are no predecessor favourites to import;
- predecessor provider enable switches and MotionBGS result/cache/quality/
  download limits have no current stored equivalent. Providers remain
  available, while custom tuning resets;
- old capture timing, one-shot video/manual-pair/Workshop selections, backend
  binary override, and custom Workshop discovery directory are not persisted.
  A custom Workshop directory is consulted during import only; and
- `sync_colors=false` has no one-to-one current field. Imported per-wallpaper
  palette policies remain active and should be reviewed in Pairings before
  first playback.

Malformed types and unknown scene scaling/clamp values stop the whole import;
they do not silently select a current default.

### Upgrade and rollback procedure

1. Back up the three legacy paths above. If a current profile exists, back up
   `~/.config/wall-in-one` and `~/.local/state/wall-in-one` too.
2. Stop using the retired full Luau plugin. Do not let it and the Rust service
   drive the desktop together.
3. Install this package, then launch the graphical app and choose one of the
   explicit migration responses. Use `--legacy-migration-status` first for a
   non-mutating check if desired.
4. Review `legacy-migration-v1.md`, missing media, the designated display,
   global cadence/renderer settings, Pairings, and schedules before enabling
   the user service.
5. Enable the packaged `wall-in-one.service` only after the resolved profile
   looks correct.

To roll back, stop and disable the current user service first. Preserve or move
aside the current config/state directories, then re-enable the old plugin. The
import deliberately leaves `config.json`, `runtime.json`, Noctalia settings,
provider markers, and old automatic stills byte-exact. Do not run both
implementations concurrently.

There is intentionally no automatic reverse conversion from the current split
stores to legacy `config.json`.

## Companion Noctalia plugin compatibility

The current application is self-contained: the GUI works without the
companion, and the packaged `wall-in-one-health-sync.timer` persists Rust crash
quarantines 30 seconds after activation and then 30 seconds after each prior
one-shot finishes. When the plugin is used, this app revision is intended only
for a coordinated release with the reviewed companion update. Thin companion
revisions through `a5e23c9` predate parts of the present runtime contract:

- they publish the top-level compatibility state but do not require status
  schema 2 or expose the authoritative independent-display route fields;
- their direct-runtime fallback never calls
  `wall-in-one --sync-runtime-health`, so a session-only crash marker is not
  durable when the packaged timer is absent; and
- they cap serialized `wall-in-one ctl` callbacks at 8 seconds. Current runtime
  actions are allowed 45 seconds because renderer handovers and bounded
  desktop helpers can run sequentially across three displays. The old timeout
  can report failure while the daemon later commits success, splitting the bar
  from actual playback state.

The matching companion revision requires status version 2, enqueues one health
sync for a visible non-durable taboo record, and raises the callback deadline
to 55 seconds (below Noctalia's 60-second clamp and above the app's 45-second
bound). Do not combine this application release with a companion revision
through `a5e23c9`. This tree's `flake.lock` already pins the reviewed candidate
`a17eb70f653afb4cf5c04afc912cdca8b14ac06e` on its non-default release branch.
Users of an older companion should prefer the packaged user service/timer and
treat the app's status UI as authoritative. The compatible app is published
first, followed immediately by promotion and tagging of companion `0.1.1`;
publishing the strict companion first would temporarily break users whose app
still emits the older status contract. This repository neither publishes nor
silently modifies the companion.
