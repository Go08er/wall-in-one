# Tidy up: handoff (branch r2-tidy, 2026-10-01)

Stopped early at the owner's request. Nothing is pushed. The main checkout
was not touched, and nothing ran against the real profile, real Noctalia
files, the service or the session bus. Real paths were only read with
`ls`/`stat`/`grep`.

## Done (each commit passed ruff, ruff format and `mypy --strict src tests` in the dev shell, plus its own test modules)

1. `70bc96c` `file_io.move_directory_no_replace`. It moves one exact
   directory: the source is pinned across a `RENAME_NOREPLACE`, and a
   replacement that wins the gap is put back. It's for archiving only and
   grants no deletion authority. 3 tests.
2. `ce4a57d` `deployed_upgrade_transaction.finished_claims()`. Read-only. It
   returns the settings and runtime replay-slot tokens, plus their parent
   directories, only when `probe()` is `complete`. The token derivation is
   shared with `_Journal` (`_role_token`). 1 test (also proves it's read-only).
3. `d738f34` `template.edit_settings(expected_sha256, transform)`,
   `read_settings_document()`, `recover_interrupted_edit()`,
   `SettingsChangedError`, and `ensure_installed_template()`, extracted from
   `install()` unchanged. 2 new tests, one with a Noctalia-style rewrite
   between read and edit. All 76 template tests pass.

Last test run, at d738f34: test_template, test_deployed_upgrade_transaction,
test_deployed_upgrade and test_file_io, 354 passed. No `nix build` gate has
been run on this branch yet.

## Left to do (design agreed with the advisor)

4. **Golden fixture.** The fixture's removal dirs `7c1d4e9a…` (config) and
   `e1f2a3b4…` (state) don't match the tokens derived from the fixture's
   completion id `e0c97044…`: `7d861fae0f1315e00d50acb1b69a9aec` (settings)
   and `d1c403e6d718801cb4798798821cae10` (runtime).
   - Rename them, and their keys in `profile.json`.
   - In `harness.reseal_deployed_markers`, read the old adoption id first,
     then rename the slot dirs from the old derived tokens to the new ones.
     The materializer re-derives the id from the sandbox paths.
   - Keep `test_relocated_deployed_evidence_is_accepted_as_complete` green.
5. **`src/wall_in_one/tidy.py`.** `plan()` must stay strictly read-only: no
   locks, never `removals.Store.operation_is_active()` (it uses O_CREAT), and
   never `recover_interrupted_edit()`. `apply(action, expected=plan)` and
   `undo(action)` work as follows:
   - They run under `legacy_migration.profile_transaction()` and re-plan.
     If the fresh plan's token differs from the previewed one, they refuse.
   - Archive: `<config>/tidy-archive/<YYYY-mm-dd-HHMMSS>-<action>/`, mode 0700,
     with `manifest.json` (applying → applied/undone), `items/NNNN-<name>`
     and a README. The manifest is written first. Renames are atomic, so
     after a crash each item is in exactly one of two places. Undo tolerates
     both, and parents are fsynced once per batch.
   - Actions: `leftovers`, `palette-template`, `old-palette-template`,
     `plugin-settings`, `thumbnail-cache` (`thumbnails.usage()`/`clear()`,
     which already exist; no archive and no undo, and the preview says so).
6. **`ui_prefs`.** Add a `tidy_offer_dismissed: bool` field to version 1.
   ui.toml has never shipped (no 0.1.5), so this isn't a version bump; say
   so in the docstring and in docs/settings.md. Update
   `test_todays_ui_never_reads_or_writes_ui_toml` deliberately: only a
   dismissal writes.
7. **Classic UI.**
   - A "Tidy up" `Adw.PreferencesGroup` at the end of `PreferencesPage`, not
     in `_settings_controls`.
   - A one-time card as an extra top bar in `MainWindow`, offered only for
     actions 1–3. Every user has a thumbnail cache, so it doesn't trigger the
     card.
   - A dedicated tidy executor in `Application`, delivering with
     `GLib.PRIORITY_DEFAULT`. Don't use `authoring_action_async`: its bare
     `idle_add` starves under a spinner. Shut it down in the existing path.
   - Gate on `authoring_ready`.
8. **New UI.** Leave a note only. AppState has two adapters and the transport
   agent is editing ui/next.
9. **Tests.**
   - Golden: for each action, preview equals result, backups are
     byte-identical, undo restores byte-identical, and a second apply is a
     no-op. Age the materialized `entry-*` dirs with `os.utime`.
   - Golden: the Noctalia rewrite between preview and apply is refused,
     leaving no backup, lock or residue.
   - GUI tests for the section and the card, in the sandboxed gui-tests check.
   - Docs: migrating.md:216-219 and library.md:441.
10. **Gates.** Run them one at a time:
    `nix build .#checks.x86_64-linux.{wall-in-one,gui-tests,golden-profile,mypy,ruff} -L --no-link`,
    then vm-test.

## Safety analysis

### 1. Archive leftovers

**Durable `.wall-in-one-removal-<token>` dirs in the config and state dirs are provably inert. Archive them.** Each is archived only when all three of these hold:
- `finished_claims()` is not None, which means the upgrade is complete;
- the token is in that role's slot set;
- the dir is in that role's parent, and its contents are a subset of `{entry}`.

Why nothing reads them after completion:
- `probe_future` returns `complete` from the marker without touching the slots.
- `ensure` → `_cleanup_completed_recovery` retires only the journal and stage.
- `_recover_claim_slots` and `_publication_states` run only on the
  journal-without-completion path.
- `_unjournaled_claim_slots_must_be_clear` runs only for a `ready`
  predecessor, and `_future_probe` short-circuits that.
- 0.1.4 has the same code.

The tokens can't be reused unless someone deletes the completion marker by
hand.

On the owner's real profile, the completion marker has adoption id `792dfb…`.
It derives `423c9fc52a56f2c857e13b95edf6fb99` (settings) and
`e478269a4fcdf040d160ea3e1173d413` (runtime). Those are exactly the config
and state dirs. They hold the pre-upgrade settings.toml (435 B) and runtime.toml
(38.8 KB), and they would be archived whole, not deleted.

**Durable dirs beside media (MotionBGS `.wall-in-one-removal-92db…`, empty).**
These come from pending-removals intents, and the tokens are random
`secrets.token_hex(16)`. Replay only follows journal intents. Archive one
only if all of these hold:
- the dir is empty;
- `pending-removals.json` reads without a fault;
- no `pending-removals.json.broken*` exists;
- no intent carries the token. The real journal has 0 intents.

Never archive a non-empty media claim: it's an interrupted deletion.

**Retained namespace entries.** Never move the `.wall-in-one-retained` container itself, only its entries. An in-flight claim holds the container by fd.
- Archive these, both provably inert:
  - empty `entry-xxxxxxxx` dirs (mkdtemp claim containers) more than one hour
    old by mtime. Claim code always makes a new mkdtemp and never reuses one.
  - regular 0-byte `entry-<32hex>` files with nlink 1 (terminal tombstones).
    Nothing reads the namespace.
- Keep, with the reason shown: anything non-empty, sockets, files with
  multiple links, and recent dirs.
- Real profile:
  - Automatic Stills: 96 empty dirs and 96 empty files. All 192 archivable.
  - MotionBGS retained: 7 dirs and 6 files, all empty. Archivable.
  - State retained: 2 dirs and 2 files. Archivable.
  - `~/.local/share/Trash/.wall-in-one-retained`: 3 empty dirs (archivable)
    and **3 non-empty trashinfo copies (158–180 B). These are kept.** They
    are hard links of the published `.trashinfo`, and nlink is now 1 because
    the trash was emptied. That would make them provably inert, but it's
    outside the agreed proof set ("0-byte"). It's the owner's call.
- Moves go into `<config>/tidy-archive`. On EXDEV (another filesystem),
  keep the entry and say why. All the real paths are on device 38.

### 2. Palette template

- Real profile: `input_path` is `…/wall-in-one/palette.json.tmpl`. Its sha256
  `ea85aad7…` is byte-identical to the bundled template. The
  content-addressed `palette-ea85aad7….json.tmpl` doesn't exist yet. The
  `# >>> managed` markers are gone, so `--install-theme-template` refuses
  this file.
- The fix:
  1. Edit only that one value, using a line edit verified by
     `tomllib(new) == tomllib(old)` with only that value changed.
  2. Run it through `edit_settings` (backup `settings.toml.bak-wall-in-one-<stamp>`,
     `.original`, the transaction record, and retained residue in Noctalia's
     state dir; the preview must list all of these).
  3. Then `noctalia.reload_config()`, which is required or Noctalia's next own
     save reverts the edit.
- Step 2b, archiving the old `.tmpl`, is offered only after palette.json is
  rewritten following the reload.
  - Noctalia skips writing when the output is unchanged
    (template_engine.cpp `previous == rendered.text`), and the two templates
    are identical. So `templates-apply` gives no evidence. Show "waiting"
    until the next color change.
  - The `config-reload` handler is a synchronous `forceReload()`.
- **R7 drop-in isn't a substitute.** Noctalia loads config-dir `*.toml` first,
  and then the state-dir `settings.toml` overrides it. The stale entry would
  still win, so settings.toml has to be edited anyway.
- Refuse unless the deployed upgrade is complete, current or absent. An open
  journal pins the Noctalia fingerprint.

### 3. Old-plugin settings

- Keys the current companion reads (0.1.3 main and 0.2.0 companion-rework)
  are kept:
  - plugin level: `refresh_interval_seconds`, `binary_path`;
  - panel shell: `controls_{placement,position,layer,open_near_click}`.
    Noctalia keys these `<panel_id>_<suffix>` (plugin_panel_shell.cpp:79).
  - widget: `display_mode`, `glyph`, `stopped_glyph`, `color`, `stopped_color`
    (per instance, kept for safety).
- Real profile: all 13 keys in the table are orphaned, including `hub_*` (the
  retired plugin's "hub" panel).
- Vendor both plugin.toml files as test fixtures, and derive the set with one
  tested function.
- Refuse while `legacy_migration.probe()` isn't absent, imported or declined:
  the importer reads these keys.
- Keep the empty table header.

### 4. Thumbnail cache

Real cache: 258,528,584 bytes. It's safe to delete and is rebuilt on demand.
`thumbnails.clear()` already exists for exactly this button.

## Known issues

- No gate has run on this branch yet.
- `HANDOFF.md` sits at the worktree root. Remove it before merging.
