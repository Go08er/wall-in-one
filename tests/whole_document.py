"""What "the runtime document builds" proved, now that the build is tolerant.

Until the build of ``runtime.toml`` became tolerant, one saved reference to a
playlist that can't play made the compile refuse, and so did a playlist
holding entries of which none resolved. The guard tests used that as their
oracle: after a refused gesture, or an allowed one, "it still builds" meant
the gesture had left no such state behind.

The compile now builds without what can't play and says so
(`runtime_config.LeftOut`), so "it builds" no longer tells a sound profile
from a broken one. `builds_whole` asks what those tests always meant: the
document is built, nothing is left out of it, and every playlist that holds
entries is in it.
"""

from __future__ import annotations

import tomllib

from wall_in_one import config, runtime_config
from wall_in_one.session import Session


def not_in_the_document(settings: config.Settings, session: Session) -> list[str]:
    """What a compile of ``session`` builds without, in words; empty for a whole document.

    Each reference left out (`runtime_config.LeftOut.warning`), and each
    playlist that holds entries and is not emitted because none of them
    resolves. Raises as the compile does for anything it still refuses.
    """
    built = runtime_config.compiled(settings, session)
    emitted = {playlist["id"] for playlist in tomllib.loads(built.document)["playlists"]}
    missing = [one.warning() for one in built.left_out]
    missing.extend(
        f"playlist {playlist.name!r} holds {len(playlist.entries)} entries, none of which resolves"
        for playlist in session.playlists.all()
        if playlist.entries and playlist.id not in emitted
    )
    return missing


def builds_whole(settings: config.Settings, session: Session) -> bool:
    """Whether the document is built with nothing left out and no playlist dropped.

    False exactly where the compile used to refuse: for a reference to a
    playlist that doesn't exist or has nothing to play, for a playlist with
    entries of which none resolves, and for everything it still refuses.
    """
    try:
        return not not_in_the_document(settings, session)
    except runtime_config.RuntimeConfigError:
        return False
