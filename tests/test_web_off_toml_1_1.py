"""A ``runtime.toml`` the service reads as TOML 1.1 is judged as it may read it (review r13f, N1).

The service's parser is the ``toml`` crate, ``1.1.6+spec-1.1.0``; the app's checks use Python's
``tomllib``, which reads TOML 1.0. TOML 1.1 adds ``\\xHH`` and ``\\e`` escapes, newlines and
trailing commas in inline tables, and times without seconds. So a hand edit such as
``"allow_web_wallpap\\x65rs" = true`` in ``[settings]`` is loaded by the service as allowing web
wallpapers, which turns its own check of each item's type off; and the app said it didn't
(``tomllib`` refuses the escape, and a document it can't parse "says nothing"), at the shutdown's
last step, at ExecStartPre's check of a kept file, and in the web-off guard's judgement of a text.

Every one of those now asks `runtime_config.allows_web_wallpapers`, which counts a document that
isn't TOML 1.0 as allowing them (fail closed), and ExecStartPre says why it refuses one.

The documents marked ``service_allows`` are the ones the service itself loads with
``allow_web_wallpapers`` true: checked with the tip's service binary, ``--check-config`` exits 0
for each, and the same document at ``schema_version = 7`` is refused with "allow_web_wallpapers
requires schema_version 8", which only a key read as true is (review r13f's ``skew/``, and this
round's run of the same check on every document here).
"""

from __future__ import annotations

import random
import tomllib

import pytest

from wall_in_one import cli, config, paths, runtime_config


def _base() -> tuple[str, str, str, str]:
    """The document that plays nothing, as the compiler writes it (schema 8, no web wallpaper),
    and its parts around ``[settings]``. Built in the test's own profile, not at import."""
    base = runtime_config.nothing_to_play_document(config.Settings(), runtime_config.WEB_OFF_REASON)
    head, settings, rest = base.partition("[settings]\n")
    assert settings and "allow_web_wallpapers" not in base
    return base, head, settings, rest


def _with(line: str, *, name: str = '"All media"') -> str:
    """The base with ``line`` first in ``[settings]``, and All media's name written as ``name``."""
    _base_text, head, settings, rest = _base()
    text = head + settings + line + "\n" + rest
    return text.replace('name = "All media"', f"name = {name}", 1)


def _as_an_inline_table_over_lines() -> str:
    """``[settings]`` as an inline table spread over lines, with a trailing comma (TOML 1.1)."""
    _base_text, head, _settings, rest = _base()
    table, blank, after = rest.partition("\n\n")
    assert blank
    keys = "".join(f"  {line},\n" for line in table.splitlines())
    return head + "settings = {\n" + keys + "  allow_web_wallpapers = true,\n}\n\n" + after


def document(name: str) -> tuple[str, bool, bool]:
    """``name``'s document, whether the service loads it allowing web wallpapers, and whether
    it is TOML 1.0."""
    if name == "x-escape-key":
        return _with('"allow_web_wallpap\\x65rs" = true'), True, False
    if name == "inline-table-over-lines":
        return _as_an_inline_table_over_lines(), True, False
    if name == "key-and-an-x-escape-elsewhere":
        return _with("allow_web_wallpapers=true", name='"All medi\\x61"'), True, False
    if name == "an-x-escape-and-no-key":
        # Not TOML 1.0, and allowing none: what can't be told apart counts as allowing them.
        return _with("", name='"All medi\\x61"'), False, False
    if name == "control-compiler-line":
        return _with("allow_web_wallpapers = true"), True, True
    if name == "control-u-escape-key":
        return _with('"allow_web_wallpap\\u0065rs" = true'), True, True
    if name == "control-off":
        return _with("allow_web_wallpapers = false"), False, True
    assert name == "control-no-key", name
    return _base()[0], False, True


DOCUMENTS: tuple[str, ...] = (
    "x-escape-key",
    "inline-table-over-lines",
    "key-and-an-x-escape-elsewhere",
    "an-x-escape-and-no-key",
    "control-compiler-line",
    "control-u-escape-key",
    "control-off",
    "control-no-key",
)


@pytest.mark.parametrize("name", DOCUMENTS)
def test_a_document_the_service_may_read_as_allowing_web_is_judged_so(name: str) -> None:
    """Before: ``[x-escape-key]``, ``[inline-table-over-lines]`` and ``[key-and-an-x-escape-
    elsewhere]`` were judged not to allow web wallpapers, which the service loads as allowing
    them. A TOML 1.0 document is judged exactly as before (the controls)."""
    text, service_allows, toml_1_0 = document(name)
    judged = runtime_config.allows_web_wallpapers(text=text)
    if toml_1_0:
        assert judged is service_allows, f"{name}: judged {judged}, the service: {service_allows}"
        return
    assert judged is True, (
        f"{name} isn't TOML 1.0, which the service may read as allowing web wallpapers "
        f"(it does: {service_allows}); judged {judged}"
    )


@pytest.mark.parametrize("name", DOCUMENTS)
def test_on_disk_the_same(name: str) -> None:
    """The file as the service reads it, through the same path every caller with no text uses."""
    text, service_allows, toml_1_0 = document(name)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    assert runtime_config.allows_web_wallpapers() is (service_allows or not toml_1_0)


@pytest.mark.parametrize("name", DOCUMENTS)
def test_which_documents_arent_toml_1_0(name: str) -> None:
    """`runtime_config.not_toml_1_0`, which ExecStartPre's refusal says (new in r13g)."""
    text, _service_allows, toml_1_0 = document(name)
    assert runtime_config.not_toml_1_0(text=text) is not toml_1_0
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    assert runtime_config.not_toml_1_0() is not toml_1_0


@pytest.mark.parametrize(
    "unreadable", ["symbolic-link", "not-utf-8", "over-the-cap", "missing"], ids=str
)
def test_control_one_the_service_cant_read_either_says_nothing(unreadable: str) -> None:
    """The service refuses these the same way (``O_NOFOLLOW``, the same 8 MiB cap, strict
    UTF-8): it loads nothing from them, so nothing is counted."""
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    allowing = document("x-escape-key")[0]
    if unreadable == "symbolic-link":
        real = target.with_name("elsewhere.toml")
        real.write_text(allowing, encoding="utf-8")
        target.symlink_to(real)
    elif unreadable == "not-utf-8":
        target.write_bytes(allowing.encode() + b"\xff")
    elif unreadable == "over-the-cap":
        padding = "#" * (runtime_config.MAX_RUNTIME_CONFIG_BYTES + 1) + "\n"
        target.write_text(allowing + padding, encoding="utf-8")
    assert runtime_config.allows_web_wallpapers() is False
    assert getattr(runtime_config, "not_toml_1_0", lambda: False)() is False


@pytest.mark.parametrize("name", DOCUMENTS)
def test_execstartpre_doesnt_keep_one_it_cant_read_as_the_service_does(name: str) -> None:
    """A kept file (the compile failed) while web wallpapers are off. Before: ``""`` for the
    three that allow them as the service reads them, and the service started on one."""
    text, service_allows, toml_1_0 = document(name)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config.save(config.Settings(allow_web_wallpapers=False))
    refusal = cli._retained_web_refusal()
    if not toml_1_0:
        assert "isn't TOML this app can read" in refusal, refusal
        assert "start the service again" in refusal
        return
    if service_allows:
        assert "allows web wallpapers" in refusal and "Allow web wallpapers is off" in refusal
        return
    assert refusal == ""


def test_control_execstartpre_with_web_on_keeps_any() -> None:
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document("x-escape-key")[0], encoding="utf-8")
    config.save(config.Settings(allow_web_wallpapers=True))
    assert cli._retained_web_refusal() == ""


# -- `runtime_config.may_spell_key`: what the end of a quit looks for unparsed (r13f, N2) ---------

KEYS = ("allow_web_wallpapers", "scene_id", "scene_base")


def _escaped(key: str, seed: int) -> str:
    """``key`` as a basic-string key, each character as itself or one of its escapes."""
    choices = random.Random(f"{key}-{seed}")
    return "".join(
        choices.choice(
            (
                character,
                f"\\u{ord(character):04x}",
                f"\\u{ord(character):04X}",
                f"\\U{ord(character):08x}",
                f"\\x{ord(character):02X}",
            )
        )
        for character in key
    )


@pytest.mark.parametrize("key", KEYS)
def test_every_spelling_of_a_key_is_found(key: str) -> None:
    """Every key a reader of either TOML finds is found, with no parse. Where TOML 1.0 can read
    the spelling (no ``\\x``), ``tomllib`` confirms it is the key."""
    for seed in range(300):
        spelled = _escaped(key, seed)
        text = f'[settings]\n"{spelled}" = true\n'
        assert runtime_config.may_spell_key(text, key), spelled
        if "\\x" not in spelled:
            assert tomllib.loads(text) == {"settings": {key: True}}, spelled
    for literal in (f"{key} = 1\n", f"'{key}' = 1\n", f"a.{key}.b = 1\n", f"[{key}]\n"):
        assert runtime_config.may_spell_key(literal, key), literal


@pytest.mark.parametrize("key", KEYS)
def test_control_a_compiled_document_without_the_key_has_none(key: str) -> None:
    """Nor any near miss: an escaped backslash before a ``u`` is a backslash, not an escape.
    (One that holds the name itself is found: a parse tells it from the key.)"""
    base = _base()[0]
    assert not runtime_config.may_spell_key(base, key)
    misses = (
        f'"{key[:-1]}"',
        f'"x{key[1:]}"',
        f'"\\\\u{ord(key[0]):04x}{key[1:]}"',  # \\u0061...: a backslash, then "u0061..."
        f'"\\u{ord(key[0]):04x}{key[1:-1]}"',
        f'"\\u00{ord(key[0]):02x}{key[1:]}\\"',  # the closing quote is escaped
    )
    for miss in misses:
        assert not runtime_config.may_spell_key(f"a = {miss}\n", key), miss
