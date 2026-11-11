"""A still a person picked for a Workshop scene, for tests that need one to play.

A web page or a preset gets no automatic still (`MediaItem.no_automatic_still`),
so a test of what compiles for one gives it the only kind it can have: a record
in the pairings file naming a picture. The picture sits outside the library
root on purpose, so it adds no still item to All media.
"""

from __future__ import annotations

from pathlib import Path

from wall_in_one.library import pairings


def pick(workshop_id: str, picture: Path) -> dict[str, pairings.Pairing]:
    """The records in which ``workshop_id`` has ``picture`` picked; the picture is made."""
    picture.parent.mkdir(parents=True, exist_ok=True)
    picture.write_bytes(b"\x89PNG")
    identity = pairings.Identity(pairings.Medium.SCENE, workshop_id)
    return {identity.key: pairings.Pairing(identity=identity, still=picture, customized=True)}


def save(*picks: dict[str, pairings.Pairing]) -> None:
    """Write ``picks`` as the pairings file of the (isolated) profile."""
    records: dict[str, pairings.Pairing] = {}
    for one in picks:
        records.update(one)
    pairings.save(records)
