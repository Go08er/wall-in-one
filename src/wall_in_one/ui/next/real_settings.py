"""The real `SettingsAccess`: the classic Settings page's paths, for the new one.

**Saving.** Every settings row saves through `Application.update_settings_async`,
the classic page's path: fields coalesced on the authoring lane, validated,
written by `config.update`, and on failure the application's own rollback
(`_fail_settings_batch`), which hands the window the durable snapshot. That
reaches the page as the ``settings`` topic, and every row then shows what
`setting` reads: ``requested_settings``, which is the durable snapshot plus
only what is still being saved, and goes back to the durable one when a save
fails. A change to the value already requested is not queued at all, so a
row put back after a failure never writes again by itself. Refusals that
happen before anything is queued (keys from a newer version, an unfinished
migration, an invalid value) are reported and put the rows back the same way.

**Off GTK.** Whether each library folder is there, the folder suggested
while there is none (``scan.default_roots`` reads Noctalia's settings), the
Wallhaven key's status (it reads the key file), whether linux-wallpaperengine
is installed, palette documents and scheme previews are worked out on worker
threads; each answer reaches GTK at ``PRIORITY_DEFAULT`` and is shown through
the ``settings`` or ``palettes`` topic. Writes stay on the authoring lane.

**Following the runtime.** While the window is open (`listen`/`close`, which
the adapter forwards), the battery note and the colour-source note follow the
status model and the attached monitors, as classic Settings does.

**Palettes.** `RealPalettes` is the classic palette browser's backend: the
discovery catalogue (`PaletteCatalog`), apply through Noctalia
(`apply_noctalia_palette_async`, then the palette is re-read), duplicate and
edit (`palettes.write_custom` / `save_edits`), and the scheme generators
(`SchemePreviewLoader`). Nothing deletes or renames a palette: the app never
has. No two palettes a person can pick share a name: a copy is named
"<name> copy" (numbered if that is taken), the editor checks a typed name
live (`palette_name_problem`), and `palettes.write_custom` refuses a clash
on the authoring lane rather than writing over anything.

**Noctalia's template.** The row reads Noctalia's settings with
`template.inspect`, which never writes, on the worker. Its button is the only
writer: `template.install` on the authoring lane, with its backups and size
check, refused while Wall-in-One's own upgrade hasn't finished. A refusal says
whether Noctalia's settings are byte-for-byte what they were. Installing never
renders: Noctalia renders the template when its colours next change, and while
it hasn't, a separate Render now press asks it to re-render all of its
templates (`render_templates`), gated the same way.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib

from wall_in_one import config, display_policy
from wall_in_one.library import scan
from wall_in_one.providers import credentials, registry
from wall_in_one.session import Session
from wall_in_one.theme import noctalia, palettes, source, template
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.theme.palette import Colour, Mode, Palette, PaletteError
from wall_in_one.ui import runtime_truth
from wall_in_one.ui.next.displays_settings import settings_edit
from wall_in_one.ui.next.settings_state import (
    KEY_FAILED,
    KEY_REFUSED,
    KEY_SAVED,
    PALETTE_ROLES,
    AppPalette,
    Folder,
    KeyStatus,
    PaletteDraft,
    PaletteItem,
    PaletteList,
    SchemePreview,
    SchemeSources,
    TemplateStatus,
    ThemeSources,
    scheme_label,
)
from wall_in_one.ui.palette_browser import SchemePreview as GeneratedScheme
from wall_in_one.ui.palette_browser import SchemePreviewLoader
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog
from wall_in_one.ui.status_model import RuntimeStatusModel, RuntimeStatusView, StatusChange
from wall_in_one.wallpaper import scenes

LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

#: The colours a swatch strip shows, as the demo and the page expect them.
STRIP_TOKENS: Final = ("surface", "primary", "secondary", "tertiary", "error")
#: How long Noctalia gets to re-render after a switch before the palette is re-read.
SETTLE_MS: Final = 600
#: A key colour a palette file doesn't carry: grey reads as unset, not as black.
UNSET_COLOUR: Final = "#808080"

PaletteReloadCallback = Callable[[source.ResolvedPalette | None, str], None]
ThemeActionCallback = Callable[[str], None]


@runtime_checkable
class SettingsBackend(Protocol):
    """What the Settings part uses of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def status_model(self) -> RuntimeStatusModel: ...

    @property
    def settings(self) -> config.Settings: ...

    @property
    def requested_settings(self) -> config.Settings: ...

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]: ...

    @property
    def resolved_palette(self) -> source.ResolvedPalette | None: ...

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool: ...

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool: ...

    def require_authoring_ready(self, *, report: bool = True) -> bool: ...

    def window_report(self, message: str) -> None: ...

    def reload_palette(
        self, on_complete: PaletteReloadCallback | None = None
    ) -> source.ResolvedPalette: ...

    def apply_noctalia_palette_async(
        self,
        palette_source: str,
        name: str,
        *,
        on_complete: ThemeActionCallback | None = None,
    ) -> bool: ...

    def use_preview_scheme(
        self,
        scheme: str,
        *,
        sync_noctalia: bool,
        on_complete: ThemeActionCallback | None = None,
    ) -> bool: ...


class Host(Protocol):
    """The adapter the part speaks through: its ``changed`` topics."""

    def emit_changed(self, *topics: str) -> None: ...


class Worker:
    """One thread for small reads; each answer is handed to GTK unless closed since.

    So is a read that failed (``failed``, with why): whatever the page shows
    while it waits ("Checking…") always ends, in what was found or in a
    "couldn't check" of its own.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._jobs: ThreadPoolExecutor | None = None
        self._generation = 0

    def run(
        self, work: Callable[[], _T], done: Callable[[_T], None], failed: Callable[[str], None]
    ) -> None:
        if self._jobs is None:
            self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._name)
        generation = self._generation
        future = self._jobs.submit(work)

        def finished(completed: Future[_T]) -> None:
            def deliver() -> bool:
                if generation != self._generation or completed.cancelled():
                    return GLib.SOURCE_REMOVE
                try:
                    result = completed.result()
                except Exception as error:  # the page is told, and the log has the rest
                    LOGGER.exception("settings: a background read failed")
                    failed(str(error) or error.__class__.__name__)
                    return GLib.SOURCE_REMOVE
                done(result)
                return GLib.SOURCE_REMOVE

            # Ends a "Checking…" the page shows: above redraw, never starved.
            GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

        future.add_done_callback(finished)

    def close(self) -> None:
        """Drop queued reads and every answer still to come."""
        self._generation += 1
        jobs, self._jobs = self._jobs, None
        if jobs is not None:
            jobs.shutdown(wait=False, cancel_futures=True)


# -- reads that touch the filesystem (worker only) ---------------------------------


@dataclass(frozen=True, slots=True)
class _FolderCheck:
    roots: tuple[Path, ...]
    missing: frozenset[Path]
    suggestion: str
    #: None when it couldn't be checked (``problem`` says why).
    scenes: bool | None
    problem: str = ""


def _check_folders(roots: tuple[Path, ...]) -> _FolderCheck:
    missing = frozenset(root for root in roots if not root.is_dir())
    found = () if roots else scan.default_roots()
    return _FolderCheck(roots, missing, str(found[0]) if found else "", scenes.is_available())


def describe_key() -> KeyStatus:
    """Say where the key in force comes from, without showing the key."""
    from_environment = bool(credentials.environment_key())
    stored = credentials.stored_key_present()
    variable = registry.API_KEY_VARIABLE
    if from_environment and stored:
        text = (
            f"Taken from {variable}, which overrides the saved key. Unset the variable "
            "to use the saved one."
        )
    elif from_environment:
        text = (
            f"Taken from {variable}. The environment is read first, so a key saved "
            "here would stay unused."
        )
    elif credentials.environment_key_is_malformed():
        # Set but unusable: "not set" would send the user looking for a key
        # they already exported, and a saved key is no way out of it.
        saved = " Unset it to use the saved key." if stored else ""
        text = (
            f"{variable} is set to something that is not a valid key, so Wallhaven "
            f"runs unauthenticated.{saved}"
        )
    elif stored:
        text = f"Saved in {credentials.key_path()}"
    else:
        text = "Not set. Searches work; NSFW results are out of reach."
    return KeyStatus(text, stored)


# -- palettes -------------------------------------------------------------------------


def role_key(role: str) -> str:
    """``on_surface_variant`` -> ``mOnSurfaceVariant``: the key a palette file carries."""
    return "m" + "".join(part.capitalize() for part in role.split("_"))


def _strip(palette: Palette | None) -> tuple[str, ...]:
    if palette is None:
        return ()
    found = (palette.colours.get(token) for token in STRIP_TOKENS)
    return tuple(colour.hex for colour in found if colour is not None)


def _entry_key(entry: palettes.PaletteEntry) -> str:
    return f"{entry.origin.value}\x00{entry.name}\x00{entry.path or ''}"


def _item(entry: palettes.PaletteEntry) -> PaletteItem:
    return PaletteItem(
        key=_entry_key(entry),
        name=entry.name,
        origin=entry.origin.value,
        detail=entry.describe(),
        dark=_strip(entry.for_mode("dark")),
        light=_strip(entry.for_mode("light")),
        can_apply=entry.origin.is_applicable,
        can_copy=entry.path is not None,
        can_edit=entry.is_editable,
    )


def _colors(document: Mapping[str, object]) -> dict[str, dict[str, str]]:
    """Each mode's role colours from a palette document; unset ones grey."""
    found: dict[str, dict[str, str]] = {}
    for mode in ("dark", "light"):
        variant = document.get(mode)
        colours: dict[str, str] = {}
        for role, _label in PALETTE_ROLES:
            raw = variant.get(role_key(role)) if isinstance(variant, dict) else None
            try:
                colours[role] = Colour.parse(raw).hex if isinstance(raw, str) else UNSET_COLOUR
            except PaletteError:
                colours[role] = UNSET_COLOUR
        found[mode] = colours
    return found


@dataclass(frozen=True, slots=True)
class _Editing:
    """A draft's handle: the document it starts from, and the entry edited in place."""

    document: Mapping[str, object]
    in_place: palettes.PaletteEntry | None = None


class RealPalettes:
    """`PaletteManager` over the palette catalogue, Noctalia and the scheme generators."""

    def __init__(self, backend: SettingsBackend, host: Host, worker: Worker) -> None:
        self._backend = backend
        self._host = host
        self._worker = worker
        self._catalog: PaletteCatalog | None = None
        self._listener = 0
        self._list = PaletteList()
        self._entries: dict[str, palettes.PaletteEntry] = {}
        self._loader: SchemePreviewLoader | None = None
        self._generated: dict[tuple[str, str], GeneratedScheme] = {}

    # -- lifetime --------------------------------------------------------------
    def open_palettes(self) -> None:
        if self._catalog is None:
            self._catalog = PaletteCatalog()
        if not self._listener:
            self._listener = self._catalog.subscribe(self._catalog_changed)
        self._catalog.ensure_loaded()

    def close_palettes(self) -> None:
        if self._catalog is not None and self._listener:
            self._catalog.unsubscribe(self._listener)
        self._listener = 0
        loader, self._loader = self._loader, None
        if loader is not None:
            loader.shutdown()
        self._generated.clear()

    def shutdown(self) -> None:
        """The window is going: stop every worker of the manager."""
        self.close_palettes()
        catalog, self._catalog = self._catalog, None
        if catalog is not None:
            catalog.shutdown()

    # -- the catalogue -----------------------------------------------------------
    def _catalog_changed(self, state: CatalogState) -> None:
        entries = state.discovery.entries
        self._entries = {_entry_key(entry): entry for entry in entries}
        self._list = PaletteList(
            phase=state.phase,
            items=tuple(_item(entry) for entry in entries),
            skipped=state.discovery.skipped,
            error=state.error,
        )
        self._host.emit_changed("palettes")

    def palette_list(self) -> PaletteList:
        return self._list

    def rescan_palettes(self) -> None:
        if self._catalog is None:
            self.open_palettes()
        else:
            self._catalog.refresh()

    def apply_catalog_palette(self, key: str) -> None:
        """Hand the palette to Noctalia, then re-read what it renders, as classic does."""
        entry = self._entries.get(key)
        report = self._backend.window_report
        if entry is None:
            report("That palette is no longer there; the list is being refreshed")
            self.rescan_palettes()
            return
        if not entry.origin.is_applicable:
            report(f"{entry.name} lives in a layout Noctalia no longer reads; duplicate it first")
            return

        def applied(error: str) -> None:
            if error:
                report(f"{entry.name} was not applied: {error}")
                return
            report(f"{entry.name} applied")
            GLib.timeout_add(SETTLE_MS, self._settle)

        self._backend.apply_noctalia_palette_async(
            entry.origin.value, entry.name, on_complete=applied
        )

    def _settle(self) -> bool:
        self._backend.reload_palette()
        return GLib.SOURCE_REMOVE

    # -- the editor ----------------------------------------------------------------
    def palette_draft(
        self, key: str, *, copy: bool, done: Callable[[PaletteDraft | None], None]
    ) -> None:
        entry = self._entries.get(key)
        if entry is None:
            self._backend.window_report(
                "That palette is no longer there; the list is being refreshed"
            )
            done(None)
            return
        if entry.path is None:
            self._backend.window_report(f"{entry.name} has no file to copy")
            done(None)
            return
        if not copy and not entry.is_editable:
            self._backend.window_report(f"{entry.name} is read-only; duplicate it to edit")
            done(None)
            return
        path = entry.path

        def read() -> dict[str, object] | str:
            try:
                return palettes.read_document(path)
            except PaletteError as error:
                return str(error)

        def opened(document: dict[str, object] | str) -> None:
            if isinstance(document, str):
                self._backend.window_report(document)
                done(None)
                return
            done(
                PaletteDraft(
                    title=f"Duplicate {entry.name}" if copy else f"Edit {entry.name}",
                    name=(
                        palettes.unique_name(f"{entry.name} copy", self._entries.values())
                        if copy
                        else entry.name
                    ),
                    renamable=copy,
                    colors=_colors(document),
                    origin=_Editing(document, None if copy else entry),
                )
            )

        def unread(error: str) -> None:
            self._backend.window_report(f"{entry.name} couldn't be read: {error}")
            done(None)

        self._worker.run(read, opened, unread)

    def save_palette_draft(
        self,
        draft: PaletteDraft,
        name: str,
        colors: Mapping[str, Mapping[str, str]],
        done: Callable[[bool], None],
    ) -> None:
        editing = draft.origin
        report = self._backend.window_report
        if not isinstance(editing, _Editing):
            report("That draft did not come from this window")
            done(False)
            return
        in_place = editing.in_place
        overrides: dict[Mode, dict[str, str]] = {
            "dark": {role_key(role): value for role, value in colors.get("dark", {}).items()},
            "light": {role_key(role): value for role, value in colors.get("light", {}).items()},
        }
        problem = self.palette_name_problem(draft, name)
        if problem:
            report(problem)
            done(False)
            return
        try:
            document = palettes.with_overrides(editing.document, overrides)
        except PaletteError as error:
            report(str(error))
            done(False)
            return
        chosen = in_place.name if in_place is not None else name.strip()

        def work() -> palettes.PaletteEntry:
            if in_place is not None:
                return palettes.save_edits(in_place, document)
            return palettes.write_custom(chosen, document)

        def saved(entry: palettes.PaletteEntry) -> None:
            done(True)  # settled first: what follows reaches the window
            if self._catalog is not None:
                # The old snapshot holds the old file: not actionable until rescanned.
                self._catalog.invalidate()
            report(f"Saved {entry.name}")

        def failed(message: str) -> None:
            done(False)  # settled first: what follows reaches the window
            report(f"The palette was not saved: {message}")

        if not self._backend.authoring_action_async(work, saved, failure=failed):
            done(False)

    # -- schemes -------------------------------------------------------------------
    def scheme_sources(self) -> SchemeSources:
        """The library's stills, starting from the one on screen (a video's paired still)."""
        session = self._backend.session
        stills = tuple(item.path for item in session.library.stills)
        cursor = session.cursor
        wanted = None
        if cursor is not None:
            wanted = cursor.paired_still if cursor.is_video else cursor.path
        selected = stills.index(wanted) if wanted in stills else 0
        return SchemeSources(tuple((str(path), path.name) for path in stills), selected)

    def preview_schemes(self, source: str, deliver: Callable[[SchemePreview], None]) -> None:
        if self._loader is None:
            self._loader = SchemePreviewLoader()
        image = Path(source)

        def arrived(generated: GeneratedScheme) -> None:
            if generated.colours is not None:
                self._generated[(source, generated.scheme)] = generated
            pair = generated.colours
            deliver(
                SchemePreview(
                    source=source,
                    scheme=generated.scheme,
                    dark=_strip(pair.dark) if pair is not None else (),
                    light=_strip(pair.light) if pair is not None else (),
                    error=generated.error,
                )
            )

        for scheme in ALL_SCHEMES:
            self._loader.request(image, scheme, arrived)

    def scheme_draft(self, source: str, scheme: str) -> PaletteDraft | None:
        generated = self._generated.get((source, scheme))
        if generated is None or generated.colours is None:
            return None
        document = palettes.document_from_pair(generated.colours)
        return PaletteDraft(
            title=f"Save {scheme_label(scheme)}",
            name=palettes.unique_name(f"{Path(source).stem} {scheme}", self._entries.values()),
            renamable=True,
            colors=_colors(document),
            origin=_Editing(document),
        )

    def palette_name_problem(self, draft: PaletteDraft, name: str) -> str:
        """The live check behind the editor's Save; `palettes.write_custom` checks again."""
        if not draft.renamable:
            return ""  # edited in place: its name is its file
        try:
            chosen = palettes.validate_name(name)
        except PaletteError as error:
            return _sentence(str(error))
        if self._list.phase in ("idle", "loading") and not self._list.items:
            return "Checking the installed palettes' names…"
        clash = palettes.name_clash(chosen, self._entries.values())
        return _sentence(palettes.name_taken_message(chosen, clash)) if clash else ""

    def use_scheme(self, scheme: str, *, sync_noctalia: bool) -> None:
        backend = self._backend
        label = scheme_label(scheme)

        def finished(error: str) -> None:
            if not error:
                backend.window_report(f"Default scheme is now {label}")
                if sync_noctalia:
                    GLib.timeout_add(SETTLE_MS, self._settle)
            elif backend.settings.preview_scheme == scheme:
                # The preference was saved before Noctalia was asked: a partial
                # success, said as one.
                backend.window_report(
                    f"Default scheme is now {label}, but Noctalia was not updated: {error}"
                )
            else:
                backend.window_report(f"The default scheme was not saved; nothing changed: {error}")
            self._host.emit_changed("settings")

        backend.use_preview_scheme(scheme, sync_noctalia=sync_noctalia, on_complete=finished)


def _sentence(text: str) -> str:
    return f"{text[:1].upper()}{text[1:]}" if text else text


# -- Noctalia's palette template (worker and authoring lane only) -------------------------


def upgrade_blocked() -> str:
    """Why Noctalia's settings must not change now (an upgrade still running), or ""."""
    from wall_in_one import deployed_upgrade_transaction

    try:
        status = deployed_upgrade_transaction.probe().status
    except Exception as error:  # a read failed: wait rather than guess
        return f"Wall-in-One couldn't check its own upgrade ({error}), so this waits."
    if status in ("absent", "current", "complete"):
        return ""
    return (
        f"Wall-in-One's own upgrade hasn't finished (its status is {status}). Noctalia's "
        "settings don't change until it has."
    )


#: What the row says while Noctalia hasn't rendered the installed template yet.
#: Noctalia renders its templates when its colours change (`template`'s own
#: account of the registration), so it isn't a promise of "soon".
NOT_RENDERED: Final = (
    " Noctalia hasn't rendered Wall-in-One's colours yet; it renders the template the "
    "next time its colours change."
)


def read_template() -> TemplateStatus:
    """The row's state from `template.inspect`, which never writes."""
    check = template.inspect()
    action = {"missing": "Install", "stale": "Repair"}.get(check.state, "")
    if not check.repairable:
        action = ""
    detail = check.detail
    can_render = check.state == "installed" and not check.rendered
    if can_render:
        detail = "Installed." + NOT_RENDERED
    blocked = upgrade_blocked() if action or can_render else ""
    return TemplateStatus(check.state, detail, action, blocked, can_render=can_render)


def _settings_digest() -> str | None:
    try:
        return hashlib.sha256(template.read_settings_document()).hexdigest()
    except template.TemplateInstallError, OSError:
        return None


def install_template() -> template.InstallResult:
    """Install or repair through `template.install`: its backups, size check and refusals.

    A refusal says whether Noctalia's settings are byte-for-byte what they were.
    Nothing is rendered here: that is `render_templates`, its own button.
    """
    blocked = upgrade_blocked()
    if blocked:
        raise template.TemplateInstallError(blocked)
    before = _settings_digest()
    try:
        result = template.install()
    except (template.TemplateInstallError, OSError) as error:
        # Said from what is on disk now, in place of the refusal's own
        # "Nothing was changed": the bytes are what the person cares about.
        reason = str(error).strip().removesuffix("Nothing was changed.").rstrip(" .;")
        unchanged = _settings_digest() == before
        raise template.TemplateInstallError(
            f"The palette template wasn't installed: {reason}. "
            + (
                "Noctalia's settings weren't changed."
                if unchanged
                else "Noctalia's settings changed while this ran; check them."
            )
        ) from error
    return result


def render_templates() -> None:
    """Render now: ask Noctalia to re-render all of its templates, not only ours.

    A separate, explicit press: installing never does this. Refused while
    Wall-in-One's own upgrade hasn't finished, as installing is.
    """
    blocked = upgrade_blocked()
    if blocked:
        raise template.TemplateInstallError(blocked)
    try:
        noctalia.apply_templates()
    except noctalia.NoctaliaError as error:
        raise template.TemplateInstallError(
            f"Noctalia didn't re-render its templates: {str(error).rstrip('.')}."
        ) from error


def _installed_sentence(result: template.InstallResult) -> str:
    if not result.changed:
        return "The palette template was already installed"
    backup = (
        f"; Noctalia's settings were backed up to {result.backup_path}"
        if result.backup_path is not None
        else ""
    )
    if result.detail == "registered":
        return f"The palette template is installed{backup}"
    return f"The palette template was repaired{backup}"


def _rendered_sentence() -> str:
    return "Noctalia was asked to re-render all of its templates"


# -- the part ---------------------------------------------------------------------------


class RealSettings:
    """`SettingsAccess` over the application, for the new Settings page."""

    def __init__(self, backend: SettingsBackend, host: Host) -> None:
        self._backend = backend
        self._host = host
        self._worker = Worker("settings-io")
        self._palettes = RealPalettes(backend, host, self._worker)
        self._folders: _FolderCheck | None = None
        self._checking: tuple[Path, ...] | None = None
        self._key = KeyStatus("Checking…")
        self._key_known = False
        self._template = TemplateStatus()
        self._template_known = False
        self._template_busy = False
        self._unsubscribe: Callable[[], None] | None = None
        self._monitors: Any = None
        self._monitor_handler = 0
        self._notes: tuple[str, ThemeSources] | None = None

    # -- lifetime --------------------------------------------------------------
    def listen(self) -> None:
        """Follow the runtime's status and the monitors (again, after `close`)."""
        if self._unsubscribe is None:
            self._unsubscribe = self._backend.status_model.subscribe(self._on_status)
        display = Gdk.Display.get_default()
        if display is not None and not self._monitor_handler:
            self._monitors = display.get_monitors()
            self._monitor_handler = self._monitors.connect("items-changed", self._on_monitors)
        self._notes_may_have_moved()

    def close(self) -> None:
        """Stop following and drop every read in flight; the window is going away."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        if self._monitor_handler and self._monitors is not None:
            self._monitors.disconnect(self._monitor_handler)
        self._monitor_handler = 0
        self._monitors = None
        self._worker.close()
        self._palettes.shutdown()
        # A read dropped now is asked for again when the page next looks.
        self._checking = None
        if self._folders is not None and self._folders.problem:
            self._folders = None
        self._key_known = False
        self._template_known = False

    def _on_status(self, _change: StatusChange, _view: RuntimeStatusView) -> None:
        self._notes_may_have_moved()

    def _on_monitors(self, *_arguments: object) -> None:
        self._notes_may_have_moved()

    def _notes_may_have_moved(self) -> None:
        notes = (self.battery_note(), self.theme_sources())
        if notes != self._notes:
            self._notes = notes
            self._host.emit_changed("settings")

    def _report(self, message: str) -> None:
        self._backend.window_report(message)

    # -- settings.toml -------------------------------------------------------------
    def setting(self, key: str) -> Any:
        return getattr(self._backend.requested_settings, key)

    def settings_blocked(self) -> str:
        keys = self._backend.settings_unknown_keys
        if not keys:
            return ""
        return (
            f"Settings are read-only. {config.read_only_message(keys)}. Correct a typo "
            "there, or change settings in the newer version that added them, then reopen "
            "Wall-in-One."
        )

    def change_settings(self, **changes: Any) -> None:
        # The display mode and the colors' display follow the rule the Displays
        # page uses too: independent displays need an attached one to follow.
        wanted = settings_edit(self._backend.requested_settings, changes, self._live_connectors())
        if isinstance(wanted, str):
            self._report(wanted)
            self._host.emit_changed("settings")
            return
        if not wanted:
            return  # what is saved or being saved already: no second write

        def failed(error: str) -> None:
            self._report(f"Settings were not saved; nothing changed: {error}")
            self._host.emit_changed("settings")

        self._backend.update_settings_async(on_error=failed, **wanted)

    # -- library folders -------------------------------------------------------------
    def _roots(self) -> tuple[Path, ...]:
        return self._backend.settings.roots

    def _check(self) -> _FolderCheck | None:
        """What is known about the saved folders; asks again when they changed."""
        roots = self._roots()
        known = self._folders
        if (known is None or known.roots != roots) and self._checking != roots:
            self._checking = roots

            def checked(result: _FolderCheck) -> None:
                if self._checking == result.roots:
                    self._checking = None
                self._folders = result
                self._host.emit_changed("settings")

            def unchecked(error: str) -> None:
                if self._checking == roots:
                    self._checking = None
                # Kept, so the page says so rather than asking again on every look;
                # forgotten when the folders change or the page next opens.
                self._folders = _FolderCheck(roots, frozenset(), "", None, problem=error)
                self._host.emit_changed("settings")

            self._worker.run(lambda: _check_folders(roots), checked, unchecked)
        return known

    def folder_list(self) -> Sequence[Folder]:
        known = self._check()
        missing = known.missing if known is not None else frozenset[Path]()
        return tuple(
            Folder(str(root), root.name or str(root), root in missing) for root in self._roots()
        )

    def suggested_folder(self) -> str:
        known = self._check()
        return known.suggestion if known is not None and not known.roots else ""

    def scenes_available(self) -> bool | None:
        known = self._check()
        return known.scenes if known is not None else None

    def folder_check_problem(self) -> str:
        known = self._folders
        return known.problem if known is not None and known.roots == self._roots() else ""

    def _set_roots(self, roots: tuple[Path, ...], saved: str = "") -> None:
        def succeeded(_settings: config.Settings) -> None:
            if saved:
                self._report(saved)

        def failed(error: str) -> None:
            self._report(f"Library folders were not saved; nothing changed: {error}")
            self._host.emit_changed("settings")

        self._backend.update_settings_async(roots=roots, on_success=succeeded, on_error=failed)

    def add_folder(self, path: str) -> None:
        """Add a folder the user chose; "Scanning" is said only once it is saved."""
        added = Path(path)
        requested = self._backend.requested_settings.roots
        if added in requested:
            self._report(f"{added.name or added} is already in the library")
            return
        self._set_roots((*requested, added), f"Scanning {added.name or added}")

    def remove_folder(self, path: str) -> None:
        removed = Path(path)
        requested = self._backend.requested_settings.roots
        if removed in requested:
            self._set_roots(tuple(root for root in requested if root != removed))

    def make_download_folder(self, path: str) -> None:
        chosen = Path(path)
        requested = self._backend.requested_settings.roots
        if chosen in requested and requested[0] != chosen:
            self._set_roots((chosen, *(root for root in requested if root != chosen)))

    # -- what the runtime and this machine report ------------------------------------
    def battery_note(self) -> str:
        """Classic's battery status row: empty while the setting is off and unenforced."""
        settings = self._backend.settings
        status = self._backend.status_model.view.status
        power = runtime_truth.power_from_status(status)
        if not (settings.stop_animations_on_battery or (power is not None and power.enabled)):
            return ""
        if power is None:
            if status is not None:
                return "Restart the updated wallpaper service to enable battery control"
            return "Waiting for the wallpaper service"
        if power.message:
            return power.message
        if not power.enabled:
            return "Waiting for the service to apply this setting"
        return "Plugged in · animations follow your playback settings"

    def _live_connectors(self) -> tuple[str, ...]:
        """GTK's monitors merged with the runtime's own display snapshot."""
        found = list(_connected_outputs())
        truth = self._backend.status_model.view.truth
        if truth is not None and truth.status_version == 2:
            for display in truth.displays:
                if display.connected and display.connector not in found:
                    found.append(display.connector)
        return tuple(found)

    def theme_sources(self) -> ThemeSources:
        settings = self._backend.settings
        session = self._backend.session
        live = self._live_connectors()
        saved = {connector for connector, _playlist in session.displays.all()}
        saved.update(rule.connector for rule in session.schedules.rules if rule.connector)
        if settings.theme_source_connector:
            saved.add(settings.theme_source_connector)
        if settings.output:
            # The legacy single Output, offered as a candidate (as classic does).
            saved.add(settings.output)
        ordered = [*live, *(connector for connector in sorted(saved) if connector not in live)]
        truth = self._backend.status_model.view.truth
        reported = truth.theme_source if truth is not None and truth.status_version == 2 else None
        resolved = display_policy.resolve_theme_source(settings.theme_source_connector, live)
        effective = reported.effective if reported is not None else resolved.effective
        fallback = reported.fallback if reported is not None else resolved.is_fallback
        if fallback:
            following = (
                f"Colours temporarily follow {effective}"
                if effective
                else "No active display is available for palette changes"
            )
            note = (
                f"{resolved.configured} is not attached. {following}; the saved choice "
                "returns when it reconnects"
            )
        elif resolved.configured:
            note = "Noctalia has one shell-wide palette; it follows this display"
        else:
            note = "Choose which display drives shell-wide colours"
        attached = set(live)
        return ThemeSources(
            tuple((connector, connector in attached) for connector in ordered),
            self._backend.requested_settings.theme_source_connector,
            note,
        )

    # -- the Wallhaven key ------------------------------------------------------------
    def _refresh_key(self) -> None:
        def known(status: KeyStatus) -> None:
            self._key = status
            self._host.emit_changed("settings")

        def unknown(error: str) -> None:
            self._key = KeyStatus(f"Couldn't check the key: {error}")
            self._host.emit_changed("settings")

        self._key_known = True
        self._worker.run(describe_key, known, unknown)

    def key_status(self) -> KeyStatus:
        if not self._key_known:
            self._refresh_key()
        return self._key

    def save_key(self, key: str, done: Callable[[str], None]) -> None:
        if not self._backend.require_authoring_ready():
            done(KEY_FAILED)
            return

        def saved(_path: Path) -> None:
            done(KEY_SAVED)  # settled first: what follows reaches the window
            self._refresh_key()
            self._report("Wallhaven API key saved")

        def failed(message: str) -> None:
            # Composed from the error's kind, never from what was typed, so no
            # path out of here can put the key on screen or into the log.
            refused = "credential:" in message
            done(KEY_REFUSED if refused else KEY_FAILED)  # settled first
            self._report(
                "That does not look like a Wallhaven API key"
                if refused
                else "The key could not be written to disk"
            )

        if not self._backend.authoring_action_async(
            lambda: credentials.save_key(key), saved, failure=failed
        ):
            done(KEY_FAILED)

    def clear_key(self) -> None:
        if not self._backend.require_authoring_ready():
            return

        def cleared(removed: bool) -> None:
            self._refresh_key()
            self._report("Wallhaven API key removed" if removed else "There was no saved key")

        def failed(message: str) -> None:
            # Safe to quote, unlike a save's: removal names the file, never a key.
            self._report(f"The saved key could not be removed: {message}")
            self._host.emit_changed("settings")

        self._backend.authoring_action_async(credentials.clear_key, cleared, failure=failed)

    # -- colours ------------------------------------------------------------------------
    def app_palette(self) -> AppPalette:
        resolved = self._backend.resolved_palette
        if resolved is None:
            return AppPalette("Not resolved yet")
        # The one case worth naming is the fallback, where the colours are ours.
        prefix = "" if resolved.is_live else "Fallback palette - "
        return AppPalette(f"{prefix}{resolved.detail}", _strip(resolved.palette))

    def reload_app_palette(self) -> None:
        def reloaded(resolved: source.ResolvedPalette | None, error: str) -> None:
            if error or resolved is None:
                return  # the application has said why
            self._report(f"Palette reloaded ({resolved.origin.value})")
            self._host.emit_changed("theme")

        self._backend.reload_palette(reloaded)

    @property
    def palette_manager(self) -> RealPalettes:
        return self._palettes

    # -- Noctalia's palette template --------------------------------------------------
    def template_status(self) -> TemplateStatus:
        if not self._template_known:
            self.check_template()
        return replace(self._template, busy=self._template_busy)

    def check_template(self) -> None:
        """Read Noctalia's settings on the worker; nothing is written."""

        def known(status: TemplateStatus) -> None:
            self._template = status
            self._host.emit_changed("settings")

        def unknown(error: str) -> None:
            self._template = TemplateStatus(
                "unavailable", f"Couldn't check Noctalia's settings: {error}"
            )
            self._host.emit_changed("settings")

        self._template_known = True
        self._worker.run(read_template, known, unknown)

    def install_template(self) -> None:
        """The row's button: `template.install` on the authoring lane, never otherwise."""
        status = self._template
        if self._template_busy:
            return
        if not status.action or status.blocked:
            self._report(status.blocked or status.detail)
            return
        if not self._backend.require_authoring_ready():
            return
        self._template_busy = True
        self._host.emit_changed("settings")

        def finished(result: template.InstallResult) -> None:
            self._template_busy = False
            self._report(_installed_sentence(result))
            self._host.emit_changed("settings")
            self.check_template()

        def failed(message: str) -> None:
            self._template_busy = False
            # The lane names an exception's type for anything but a ValueError;
            # this sentence already says everything.
            self._report(message.removeprefix(f"{template.TemplateInstallError.__name__}: "))
            self._host.emit_changed("settings")
            self.check_template()

        if not self._backend.authoring_action_async(install_template, finished, failure=failed):
            self._template_busy = False
            self._host.emit_changed("settings")

    def render_templates(self) -> None:
        """Render now: the only caller of `noctalia.apply_templates` here, and only on a press."""
        status = self._template
        if self._template_busy:
            return
        if not status.can_render or status.blocked:
            self._report(status.blocked or status.detail)
            return
        if not self._backend.require_authoring_ready():
            return
        self._template_busy = True
        self._host.emit_changed("settings")

        def finished(_result: None) -> None:
            self._template_busy = False
            self._report(_rendered_sentence())
            self._host.emit_changed("settings")
            self.check_template()
            # Noctalia renders after it answers; look once more when it has had a moment.
            GLib.timeout_add(SETTLE_MS, self._check_again)

        def failed(message: str) -> None:
            self._template_busy = False
            self._report(message.removeprefix(f"{template.TemplateInstallError.__name__}: "))
            self._host.emit_changed("settings")
            self.check_template()

        if not self._backend.authoring_action_async(render_templates, finished, failure=failed):
            self._template_busy = False
            self._host.emit_changed("settings")

    def _check_again(self) -> bool:
        self.check_template()
        return GLib.SOURCE_REMOVE


def _connected_outputs() -> tuple[str, ...]:
    """Connector names of the monitors attached right now, in GTK's order."""
    display = Gdk.Display.get_default()
    if display is None:
        return ()
    monitors = display.get_monitors()
    found = []
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        connector = monitor.get_connector() if isinstance(monitor, Gdk.Monitor) else None
        if connector:
            found.append(connector)
    return tuple(found)
