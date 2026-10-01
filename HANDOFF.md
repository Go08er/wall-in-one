# r2-timing handoff

Branch `r2-timing`, rebased on `ui-integration` 369874d (the transport-controls
merge). Not pushed.

## Done

- **Rust (additive, status stays v2).** Every display row gains, appended after
  the released fields:
  - `route_change_at` (local `YYYY-MM-DDTHH:MM:SS`) and `route_change_in_s`:
    the next schedule boundary that changes the display's automatic
    (playlist, route_source);
  - `next_cycle_at` and `next_cycle_in_s`: the rotation deadline;
  - the companion's names, `until` (`HH:MM`, only under 24 h away) and
    `next_change_in_s` (equal to `next_cycle_in_s`).
  `schedule::next_change` examines only midnight and each enabled rule's start
  and end minute, at most 8 days ahead, under a budget of 2M rule checks per
  status reply. The rules are documented in docs/runtime-config.md ("Route and
  rotation timing").
- **Precedence and edge cases** (each tested in service/tests/service.rs):
  - a manual pick has no route change but keeps its rotation deadline;
  - paused has no deadline, but its route change is still reported; stopped
    keeps cycling;
  - mirrored rows share one timer, and the opt-in is dormant there;
  - in independent mode the precedence includes `beats_global_rules`;
  - detached rows are all null;
  - a handover between two rules with the same playlist and source is not a
    change;
  - midnight and wrapped windows are handled.
- **Byte identity.** `older_status_fields_stay_byte_identical` compares four
  status shapes with the recorded fixture service/tests/fixtures/status-shape-v2.txt,
  which was recorded before any change.
- **Python.** runtime_truth parses the new fields. They are None when absent,
  and a malformed pair is dropped without rejecting the snapshot.
- **New UI** (`_read_player` only):
  - the reason gets the runtime's `until` for every route but a pick;
  - timing is "Next in N min" (the soonest deadline in scope, rounded up,
    never 0); "Not changing" is kept.
- **Coordinator's fix.** A paused-plus-stopped mix now reads as `paused`, so
  Play shows Play (its `toggle` plays all). It is tested with the verb Play
  sends.
- **Companion.** `ctl status` prints the Rust reply verbatim, so `until` and
  `next_change_in_s` reach the companion directly. They are pinned in Rust
  (`companion_status_fields_keep_their_wire_names_and_types`) and on the real
  `ctl status` path (tests/test_runtime_socket_fallback.py).
- **Binary size.** chrono's formatter and serde flatten were avoided, so the
  service binary grew by about 16 KB instead of 37 KB.

## Gates

On e436a15 (the code tip; this file is the only later commit), each gate run
one at a time:

| Gate | Result |
|---|---|
| wall-in-one-service | pass (118 integration, 32 unit, 30 config and the rest) |
| wall-in-one | pass (2944 passed, 34 skipped) |
| gui-tests | pass (402) |
| golden-profile | pass (66 passed, 27 skipped) |
| mypy | pass |
| ruff | pass |
| companion-plugin-contract | pass, cached: its inputs are not changed by this branch |
| runtime-socket-fallback | pass (4) |
| service-rss | SERVICE_RSS_RESULT |

cargo fmt --check and clippy `-D warnings` are clean.

vm-test was **not** run. It was stopped before it started, at the owner's
wrap-up. Run it once on this tip: `nix build .#checks.x86_64-linux.vm-test -L --no-link`.

## Not published to the companion, and why

- **`entry_name`, `thumbnail`, `cover_thumbnails`.** There is no channel to
  the runtime:
  - runtime.toml is strict (`deny_unknown_fields`);
  - any addition to runtime-overrides.toml must bump its `schema_version`,
    and older services ignore the whole file then.
  The future route is overrides schema 2, or a new sidecar.
- **`open_pages`.** Neither UI can open any page the companion asks about:
  - both UIs' `ctl open` accepts only the seven base aliases, and the new UI
    opens only `media`;
  - the companion's `advertised()` checks only the deep pages
    (`playlist:`, `displays:`, `library:problems`, `settings:battery`,
    `settings:log`);
  - the runtime cannot know which UI is running.
- **No status file is written.** So the "write only on change" and golden
  idle-test clauses have nothing to apply to.
- **No companion contract test file exists in the app repo.** The companion
  gate runs the companion repo's own tests.

## Known issues

- **Status wire budget.** The six fields add at most about 20.6 KiB across 128
  rows, which pushes the worst-case structure past the nominal 256 KiB
  `STATUS_STRUCTURAL_RESERVE`. That reserve was left unchanged, as it was for
  the earlier power and overrides fields: raising it would make the new
  service refuse a runtime.toml that an older one loads. A max-count status
  measured about 280 KB, against the 1 MiB cap.
- **DST.** The seconds come from naive local times and can be an hour off
  across a daylight-saving change. The wall-clock fields are the authority.
- **Budget.** Past 2M rule checks per reply (hundreds of rules on dozens of
  displays), the remaining displays report a null route change.
