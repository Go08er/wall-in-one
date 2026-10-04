"""Would a change to the library break playback? Do it for pretend, then compile.

This is the guarantee behind every change that can take away what a playlist
plays: deleting a wallpaper (and what goes with it), removing a library
folder, or turning off Wallpaper Engine scanning. On the authoring worker,
after the change's GTK ``prepare`` and before anything is moved, deleted or
saved, the real thing runs on copies:

1. the real scanner scans the library as the change would leave it
   (`library.scan.scan`; for a removal, with the files it deletes excluded by
   file identity, so a path reaching one through a linked folder is gone
   too);
2. the real cleanup runs on copies of the stores (a removal clears the
   pairings, playlist entries and star of what it deletes);
3. the compiler's own rule judges every playlist before and after
   (`runtime_config.change_refusal` over `entry_playability`), and the real
   compiler (`runtime_config.render`) has the last word.

A change that would leave a playlist something uses with nothing to play, or
any playlist with entries holding nothing the compiler can use, is refused,
naming the playlist and why (the wallpaper whose still goes, the still it
plays through). The scanner is the specification, so nothing here predicts
what it would find; the checks pages make as the gesture happens are early
feedback only.

A dry run writes nothing: the scan, the adoption manifest it reads and every
still convention only read, and the copies are never saved. When a removal
then commits, the library it scanned is the post-change library, adopted as
it is, without another scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from wall_in_one import config, runtime_config
from wall_in_one.library import (
    adopted,
    displays,
    favourites,
    manage,
    pairing,
    pairings,
    playlists,
    removals,
    scan,
    schedules,
    workshop,
)
from wall_in_one.library.model import Library, MediaItem
from wall_in_one.library.playlists import Playlist
from wall_in_one.session import Scanner, Session


class CheckFailed(ValueError):  # noqa: N818 - reads as the sentence it carries
    """The dry run itself could not run, so the change is not made."""

    kind = "local-io"


@dataclass(frozen=True, slots=True)
class Uses:
    """What uses playlists besides the settings: schedule rules and displays' own.

    Taken from the live session on GTK, as values, so a dry run on the
    worker counts the uses the change is made against.
    """

    rules: tuple[schedules.Rule, ...] = ()
    assignments: tuple[tuple[str, str], ...] = ()
    beating: frozenset[str] = frozenset()

    @classmethod
    def of(cls, session: Session) -> Uses:
        assignments = session.displays.all()
        return cls(
            rules=session.schedules.rules,
            assignments=assignments,
            beating=frozenset(
                connector
                for connector, _playlist in assignments
                if session.displays.beats_global_rules(connector)
            ),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a dry run found: everything the change would leave, and what it breaks."""

    #: The library as the change leaves it, as the scanner finds it.
    library: Library
    #: The pairings, playlists and favourites as it leaves them (a removal's
    #: cleanup clears what named what it deletes).
    records_after: Mapping[str, pairings.Pairing]
    playlists_after: tuple[Playlist, ...]
    favourites_after: frozenset[Path]
    #: The runtime configuration the compiler builds then, or why it can't
    #: (`runtime_config.render`); exactly one is non-empty.
    compiled: str
    compile_error: str
    #: One clause per playlist the change would break (`runtime_config.listed`
    #: joins them); empty when the change may be made.
    refusals: tuple[str, ...]


def removal(
    item: MediaItem,
    *,
    settings: config.Settings,
    library: Library,
    roots: Sequence[Path],
    adopted_authority: adopted.Authority | None,
    favourite_store: favourites.Store,
    pairing_store: pairings.Store,
    playlist_store: playlists.Store,
    uses: Uses,
    scanner: Scanner | None = None,
) -> Verdict:
    """Deleting ``item`` for pretend: what the library and every playlist would be.

    ``library`` is the accepted library; the stores are this worker's durable
    copies, and ``uses`` the schedule rules and display playlists. The files
    excluded are the ones the removal itself deletes, as it derives them
    before touching anything: the wallpaper, its provider sidecars, and its
    pairing artifacts (its pairing sidecar, its captures and, with a
    deletion-capable adoption, its adopted capture and sidecar;
    `manage.pairing_artifact_paths`). Of those, only regular files are
    excluded, as the removal deletes only regular files; one the removal
    keeps after all (unreadable, say) is excluded anyway, which can only
    refuse more.
    """
    records = dict(pairing_store.records)
    saved = records.get(pairings.Identity.of(item).key)
    capable = (
        adopted_authority
        if adopted_authority is not None
        and adopted_authority.adoption_id
        and adopted_authority.sidecar_fingerprint is not None
        else None
    )
    pairing_paths = manage.pairing_artifact_paths(
        item,
        roots,
        legacy_selected_still=saved.still if saved is not None else None,
        adopted_authority=capable,
    )
    gone = pairing.Exclusion.of(
        (item.path, *manage.provider_sidecar_paths(item.path), *pairing_paths)
    )
    # The removal's own cleanup, on copies: what `Session._cleanup_removed_item`
    # does to the stores once the file is gone.
    cleaned_pairings = pairings.Store(records)
    cleaned_pairings.adopt_worker_forget_item(item, removed_stills=pairing_paths)
    cleaned_playlists = playlists.Store({one.id: one for one in playlist_store.all()})
    cleaned_playlists.adopt_worker_forget_path(item.path)
    cleaned_favourites = favourites.Store(favourite_store.favourites)
    cleaned_favourites.adopt_worker_discard(item.path)
    if scanner is None:
        after_library = _scan(settings, cleaned_pairings.records, excluded=gone)
    else:
        # An injected scanner is a test double handing over a ready library,
        # which can't honor identities: the accepted one, less the wallpaper.
        after_library = replace(
            library,
            items=tuple(one for one in library.items if one.path != item.path),
            still_inventory=tuple(one for one in library.still_inventory if one.path != item.path),
        )
    stores_before = _Stores(pairing_store, playlist_store, favourite_store)
    stores_after = _Stores(cleaned_pairings, cleaned_playlists, cleaned_favourites)
    judged = _judge(
        (settings, library, stores_before), (settings, after_library, stores_after), uses, gone
    )
    if judged.refusals and scanner is None:
        # Blame only what this removal breaks: against the library as it is on
        # disk now, not as last accepted, in case something else went meanwhile.
        on_disk = _scan(settings, records, excluded=None)
        judged = _judge(
            (settings, on_disk, stores_before), (settings, after_library, stores_after), uses, gone
        )
    return _verdict(after_library, stores_after, judged)


def sources(
    before: config.Settings,
    after: config.Settings,
    *,
    library: Library,
    pairing_store: pairings.Store,
    playlist_store: playlists.Store,
    favourite_store: favourites.Store,
    uses: Uses,
    scanner: Scanner | None = None,
) -> Verdict:
    """Saving ``after``'s library folders and Wallpaper Engine scanning, for pretend.

    The real scan of the folders asked for, with the pairings as they are:
    whatever it finds, including what it no longer finds (a wallpaper outside
    them, a capture in a folder no longer scanned, an adoption whose own
    folder is no longer one), is what the next scan will find. Playlist
    entries stay, naming what is no longer there. Uses are counted as
    ``after`` has them.
    """
    records = dict(pairing_store.records)
    stores = _Stores(pairing_store, playlist_store, favourite_store)
    after_library = (
        _scan(after, records, excluded=None) if scanner is None else scanner(after.roots)
    )
    judged = _judge((before, library, stores), (after, after_library, stores), uses, None)
    if judged.refusals:
        on_disk = (
            _scan(before, records, excluded=None) if scanner is None else scanner(before.roots)
        )
        judged = _judge((before, on_disk, stores), (after, after_library, stores), uses, None)
    return _verdict(after_library, stores, judged)


# -- the parts -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stores:
    pairings: pairings.Store
    playlists: playlists.Store
    favourites: favourites.Store


@dataclass(frozen=True, slots=True)
class _Judged:
    refusals: tuple[str, ...]
    compiled: str
    compile_error: str


def _verdict(library: Library, stores: _Stores, judged: _Judged) -> Verdict:
    return Verdict(
        library=library,
        records_after=dict(stores.pairings.records),
        playlists_after=stores.playlists.all(),
        favourites_after=stores.favourites.paths,
        compiled=judged.compiled,
        compile_error=judged.compile_error,
        refusals=judged.refusals,
    )


def _scan(
    settings: config.Settings,
    records: Mapping[str, pairings.Pairing],
    *,
    excluded: pairing.Exclusion | None,
) -> Library:
    """The scan `Session.prepare_scan` would run for ``settings``, honoring ``excluded``."""
    include_workshop = settings.scan_workshop and bool(settings.roots)
    return scan.scan(
        settings.roots,
        dict(records),
        include_workshop=include_workshop,
        workshop_roots=workshop.steam_roots() if include_workshop else (),
        excluded=excluded,
    )


def _session(settings: config.Settings, library: Library, stores: _Stores, uses: Uses) -> Session:
    """A Session over copies, for the compiler: nothing in it is live or saved."""
    made = Session(
        settings,
        pairing_store=pairings.Store(dict(stores.pairings.records)),
        playlist_store=playlists.Store({one.id: one for one in stores.playlists.all()}),
        favourite_store=favourites.Store(stores.favourites.favourites),
        schedule_store=schedules.Store(uses.rules),
        display_store=displays.Store(dict(uses.assignments), beats_global_rules=uses.beating),
        removal_store=removals.Store(),
        owns_playback=False,
    )
    made.adopt_library(library, reconcile_workshop=False)
    return made


def _judge(
    was: tuple[config.Settings, Library, _Stores],
    will_be: tuple[config.Settings, Library, _Stores],
    uses: Uses,
    gone: pairing.Exclusion | None,
) -> _Judged:
    """Each playlist the change would break, as a clause saying which and why; and
    what the compiler builds after it."""
    settings_after = will_be[0]
    before = _session(*was, uses)
    after = _session(*will_be, uses)
    try:
        known_before = runtime_config.library_index(before)
        known_after = runtime_config.library_index(after)
        found: list[str] = []
        for playlist in after.playlists.all():
            previous = before.playlists.get(playlist.id)
            entries_before = previous.entries if previous is not None else ()
            can_before = runtime_config.playlist_playability(entries_before, before, known_before)
            can_after = runtime_config.playlist_playability(playlist.entries, after, known_after)
            refusal = runtime_config.change_refusal(
                f"\u201c{playlist.name}\u201d",
                can_before,
                can_after,
                runtime_config.playlist_uses(playlist.id, settings_after, after),
            )
            if not refusal:
                continue
            compiled_before = {
                entry.id
                for entry, can in zip(entries_before, can_before, strict=True)
                if can.compiles
            }
            reasons = [
                _why(entry.path, before, known_before, known_after, gone)
                for entry, can in zip(playlist.entries, can_after, strict=True)
                if entry.id in compiled_before and not can.compiles
            ]
            because = "; ".join(dict.fromkeys(reasons))
            found.append(f"{refusal}: {because}" if because else refusal)
        try:
            compiled, compile_error = runtime_config.render(settings_after, after), ""
        except runtime_config.RuntimeConfigError as error:
            compiled, compile_error = "", str(error)
        if compile_error and not found:
            # The compiler has the last word on everything else it checks.
            try:
                runtime_config.render(was[0], before)
            except runtime_config.RuntimeConfigError:
                pass  # already unbuildable: not this change's doing
            else:
                found.append(compile_error)
        return _Judged(tuple(found), compiled, compile_error)
    finally:
        before.shutdown()
        after.shutdown()


def _why(
    source: Path,
    before: Session,
    known_before: dict[Path, MediaItem],
    known_after: dict[Path, MediaItem],
    gone: pairing.Exclusion | None,
) -> str:
    """Why one entry that played before would not, in a person's words."""
    if known_after.get(source) is None:
        return f"{source.name} would no longer be in the library"
    was = known_before.get(source)
    still = before.pairings.resolve_accepted(was, before.library).still if was is not None else None
    if still is None or still == source:
        return f"{source.name} would have no still for playback to show"
    if gone is not None and gone.removes(still):
        return f"{source.name} plays through {still.name}, which this removes"
    return f"{source.name} plays through {still.name}, which would no longer be its still"
