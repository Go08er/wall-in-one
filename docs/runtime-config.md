# Runtime configuration contract

`wall-in-one-service` reads one input: a fully resolved TOML document written
by the Python application. Schema version `1` is intentionally strict. An
unknown version, unknown field, relative path, dangling playlist reference, or
kind-specific entry missing its motion source makes the service refuse to
start. It never falls back to the application's database or library files.

The default path is `$XDG_STATE_HOME/wall-in-one/runtime.toml` (normally
`~/.local/state/wall-in-one/runtime.toml`). `--config PATH` overrides it. The
app writes a same-directory temporary file, flushes it, and renames it over the
destination. The service reloads only a complete new document; a failed reload
keeps the last valid configuration.

Schema 1 contains `schema_version`, `default_playlist`, `[settings]`,
`[renderer]`, `[[playlists]]`, `[[schedules]]`, and `[[displays]]`. Executable
and media paths are absolute. Every playlist entry has a stable `id`, `kind`,
absolute `still`, and inline `palette`. A video additionally has an absolute
`motion`; a scene has a numeric `scene_id`.

```toml
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "auto" }
palette = { kind = "named", source = "community", name = "catppuccin", mode = "dark" }
palette = { kind = "keep", mode = "keep" }
```

The allowed modes are `keep`, `dark`, `light`, and `auto`; named sources are
`builtin`, `community`, and `custom`. The service does not read palette files.
It forwards the already-selected source and name to Noctalia.

Schedule months use `1..12`; weekdays use Monday=`0` through Sunday=`6`.
Times are local `HH:MM`, inclusive at the start and exclusive at the end. An
end earlier than its start wraps midnight. Empty month/weekday lists and an
absent time window mean “any”. Rules stay ordered because the last match wins.

This file contains configuration only. Cursor, pause state, shuffle order, the
manual playlist override, renderer PIDs, and current status are runtime state
and never get written into it.
