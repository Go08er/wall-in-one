"""Picklable provider parsing entry points for the shared backend pool.

Nothing here owns an HTTP client, cache, GTK object or mutable application
state. Every input arrives as plain data. Imports happen inside each worker so
a subinterpreter constructs its own provider module state rather than depending
on the parent interpreter's globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wall_in_one.providers.motionbgs import ListingPage, MotionBgsDetail


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """A provider error in a form that is reliably picklable."""

    kind: str
    message: str


def _failure(error: Exception) -> ProviderFailure:
    kind = getattr(error, "kind", "provider")
    message = error.args[0] if error.args else str(error)
    return ProviderFailure(str(kind), str(message))


def motionbgs_listing(
    markup: str,
    mode: str,
    query: str,
    genre: str,
    page: int,
    source_url: str,
    limit: int,
) -> ListingPage | ProviderFailure:
    """Parse one bounded MotionBGS listing without sharing the UI's GIL."""
    from wall_in_one.providers.base import ProviderError
    from wall_in_one.providers.motionbgs import parse_listing

    try:
        return parse_listing(
            markup,
            mode=mode,
            query=query,
            genre=genre,
            page=page,
            source_url=source_url,
            limit=limit,
        )
    except ProviderError as error:
        return _failure(error)


def motionbgs_detail(markup: str, slug: str) -> MotionBgsDetail | ProviderFailure:
    """Parse one bounded MotionBGS detail page without sharing the UI's GIL."""
    from wall_in_one.providers.base import ProviderError
    from wall_in_one.providers.motionbgs import parse_detail

    try:
        return parse_detail(markup, slug)
    except ProviderError as error:
        return _failure(error)


def value[T](result: T | ProviderFailure) -> T:
    """Reconstruct an expected provider failure in the calling interpreter."""
    if isinstance(result, ProviderFailure):
        from wall_in_one.providers.base import ProviderError

        raise ProviderError(result.kind, result.message)
    return result


def _probe(value: str) -> tuple[str, int]:
    """Return the worker interpreter id so tests prove work really ran."""
    from concurrent import interpreters

    return value, int(interpreters.get_current().id)
