"""What the released 0.2.2 leaves of a template transaction, and this version after it.

Review rule 4, run rather than read (AB-1's class, outside downloads). The
released `theme.template` is the producer, in a child (`tests.released`): it
installs, or uninstalls, the palette template in Noctalia's settings, through
its backup, lock, candidate, locator and the one exchange. Each run takes one
fault (`tests.released_faults`): a kill before every change it makes, one EIO
or EACCES at every call, and an edit of what it just made public, alone or
followed by a failure or a kill. Then a person may edit any one file it left.

This checkout is the consumer, doing what it ordinarily does next: it looks
(`inspect`), recovers (as Tidy up does before a change), installs, runs Tidy
up's survey and every action it offers, and uninstalls. Noctalia's settings
are a person's file: nothing that was ever public, and nothing a person
edited, may lose a byte, wherever it was left; and every path this version
names in what it says is there.
"""

from __future__ import annotations

import os
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import tidy
from wall_in_one.theme import noctalia, template

#: The released producer. Its job's argument: ``install``, or ``uninstall``
#: (of a template the released code installed first, unfaulted).
PRODUCER = r"""
from wall_in_one import file_io
from wall_in_one.theme import noctalia, template


def _no_noctalia(*_arguments, **_keywords):
    raise noctalia.NoctaliaError("no Noctalia in a probe")


noctalia._run = _no_noctalia
trace_filesystem((file_io, "_rename_noreplace"), (template, "_rename_exchange"))


def operation(name):
    try:
        if name == "uninstall":
            disarm()
            template.install(reload_config=False)
            arm()
            result = template.uninstall(reload_config=False)
        else:
            result = template.install(reload_config=False)
        print("DONE", result.detail, flush=True)
    except template.TemplateInstallError as error:
        print("REFUSED", error, flush=True)
    except OSError as error:
        print("ERROR", error, flush=True)


serve(operation)
"""

#: Noctalia's settings as a person has them, before Wall-in-One touches them.
SETTINGS = '# my own notes on this bar\n[theme]\nsource = "wallpaper"\n\n[bar]\nposition = "top"\n'
#: A day and an hour: past every wait Tidy up has.
LATER_SECONDS = 25 * 3600


def _prepare(base: Path) -> faults.Tree:
    """A profile whose Noctalia has settings; the tree a person had before anything ran."""
    settings = Path(faults.profile_environment(base)["XDG_STATE_HOME"]) / "noctalia"
    settings.mkdir(parents=True)
    (settings / "settings.toml").write_text(SETTINGS, encoding="utf-8")
    return faults.tree(base)


def _installed(result: template.InstallResult) -> str:
    for path in (result.settings_path, result.backup_path):
        assert path is None or path.is_file(), f"{path} is named in the result, and isn't there"
    return result.detail


def _consume(base: Path) -> Iterator[tuple[str, str]]:
    """This version's ordinary operations, in order; each step's name and what it said."""
    later = time.time() + LATER_SECONDS

    def look() -> str:
        before = faults.shape(faults.tree(base), base)
        said = template.inspect().detail
        assert faults.shape(faults.tree(base), base) == before, "looking writes nothing"
        return said

    def tidy_up() -> str:
        said = []
        for action in tidy.plan(roots=(), now=later).actions:
            if action.action in tidy.OFFERED and action.ready:
                said.append(tidy.apply(action.action, action, roots=()).message)
        return " / ".join(said)

    def recover() -> str:
        try:
            found = template.recover_interrupted_edit()
        except template.TemplateInstallError as error:
            return f"REFUSED {error}"
        return "found and settled one" if found else "nothing to recover"

    # Looking and recovering agree: "interrupted" is said exactly when there
    # is something for a repair to finish, undo, or refuse to touch.
    looked = look()
    yield "inspect", looked
    recovered = recover()
    said_interrupted = "was interrupted" in looked
    plainly = looked.startswith(("Installed", "Not installed"))
    if said_interrupted and recovered == "nothing to recover":
        recovered = f"FINDING it said {looked!r}, and there was nothing to recover"
    elif plainly and recovered != "nothing to recover":
        recovered = f"FINDING it said {looked!r}, and recovering said: {recovered}"
    yield "recover", recovered

    steps: tuple[tuple[str, Callable[[], str]], ...] = (
        ("install", lambda: _installed(template.install(reload_config=False))),
        ("tidy up", tidy_up),
        ("uninstall", lambda: _installed(template.uninstall(reload_config=False))),
    )
    for name, step in steps:
        try:
            yield name, step()
        except (template.TemplateInstallError, tidy.TidyError) as error:
            yield name, f"REFUSED {error}"


def _campaign(
    operation: str,
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )

    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        (operation,),
        tmp_path,
        prepare=_prepare,
        consume=_consume,
        full=full,
    )

    print("\n" + found.summary(f"template {operation}"))
    # The state this is all for was among them: the process died right after
    # the exchange, and the person's settings sat at the candidate's name.
    displaced = [
        state
        for state in found.states
        if any("transaction-candidate" in name and size == len(SETTINGS) for name, size in state)
    ]
    if operation == "install":
        assert displaced, "no state had the displaced settings at the candidate's name"
    assert not found.findings, "\n".join(sorted(set(found.findings))[:40])


def test_nothing_the_released_template_install_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs: the install, under the bounded plans."""
    _campaign("install", released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_nothing_the_released_template_transaction_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Every plan; and the uninstall too (the same transaction, over a file that
    already holds this app's block)."""
    _campaign(operation, released_source, tmp_path, monkeypatch, full=True)
