"""Live color provenance and safe degraded palettes, without shell IPC."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wall_in_one import paths
from wall_in_one.theme import css, noctalia, source
from wall_in_one.theme.palette import PalettePair


def _registration(*, enabled: bool = True, output: Path | None = None) -> None:
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    template = settings.parent / "palette.tmpl"
    template.write_text("template", encoding="utf-8")
    settings.write_text(
        "[theme.templates.user.wall-in-one]\n"
        f"enabled = {str(enabled).lower()}\n"
        f"input_path = {json.dumps(str(template))}\n"
        f"output_path = {json.dumps(str(output or paths.palette_path()))}\n",
        encoding="utf-8",
    )


def _render_palette() -> Path:
    target = paths.palette_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "mode": "dark",
                "colors": {
                    key: value.hex for key, value in source.fallback_palette().colours.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return target


def test_live_wallpaper_generation_prefers_the_shells_actual_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallpaper = tmp_path / "wallpaper.png"
    wallpaper.touch()
    calls: list[str] = []
    pair = PalettePair(source.fallback_palette(), source.fallback_palette("light"))
    monkeypatch.setattr(noctalia, "current_wallpaper", lambda **_kwargs: wallpaper)
    monkeypatch.setattr(
        noctalia,
        "current_scheme_selection",
        lambda **_kwargs: SimpleNamespace(source="wallpaper", name="m3-expressive"),
    )
    monkeypatch.setattr(noctalia, "current_mode", lambda **_kwargs: "light")

    def generate(_path: Path, scheme: str, **_kwargs: object) -> PalettePair:
        calls.append(scheme)
        return pair

    monkeypatch.setattr(noctalia, "generate", generate)
    resolved = source.resolve(scheme="m3-tonal-spot")
    assert calls == ["m3-expressive"]
    assert resolved.origin is source.Origin.GENERATED
    assert resolved.palette is pair.light
    assert not resolved.is_live
    assert "approximate" in resolved.detail
    assert "not registered" in resolved.detail


def _mock_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[tuple[Path, str, bool]]]:
    wallpaper = tmp_path / "default.png"
    wallpaper.touch()
    calls: list[tuple[Path, str, bool]] = []
    monkeypatch.setattr(noctalia, "current_wallpaper", lambda **_kwargs: wallpaper)
    monkeypatch.setattr(
        noctalia,
        "current_scheme_selection",
        lambda **_kwargs: noctalia.ColourSchemeSelection("wallpaper", "m3-content"),
    )
    monkeypatch.setattr(noctalia, "current_mode", lambda **_kwargs: "dark")
    monkeypatch.setattr(noctalia, "is_available", lambda: True)

    def generate(
        image: Path,
        scheme: str,
        *,
        pure_black: bool = False,
        cancelled: source.CancelCheck | None = None,
    ) -> PalettePair:
        calls.append((image, scheme, pure_black))
        return PalettePair(source.fallback_palette(), source.fallback_palette("light"))

    monkeypatch.setattr(noctalia, "generate", generate)
    return wallpaper, calls


@pytest.mark.parametrize("home_relative", [False, True])
def test_generation_prefers_the_saved_palette_driving_wallpaper(
    home_relative: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, calls = _mock_generation(tmp_path, monkeypatch)
    last = Path.home() / "last.png"
    last.parent.mkdir(parents=True)
    last.touch()
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(
        f"[wallpaper.last]\npath = {json.dumps('~/last.png' if home_relative else str(last))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        noctalia,
        "current_wallpaper",
        lambda **_kwargs: pytest.fail("saved palette image does not need the default image IPC"),
    )

    resolved = source.from_current_wallpaper()

    assert resolved is not None
    assert calls == [(last, "m3-content", False)]
    assert "last.png" in resolved.detail


@pytest.mark.parametrize("invalid", ["missing", "directory", "relative", "wrong-type", "nul"])
def test_unusable_saved_palette_image_falls_back_to_the_shell_default(
    invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default, calls = _mock_generation(tmp_path, monkeypatch)
    invalid_paths: dict[str, str | int] = {
        "missing": str(tmp_path / "gone.png"),
        "directory": str(tmp_path),
        "relative": "relative.png",
        "wrong-type": 9,
        "nul": "\0",
    }
    path = invalid_paths[invalid]
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(f"[wallpaper.last]\npath = {json.dumps(path)}\n", encoding="utf-8")

    assert source.from_current_wallpaper() is not None
    assert calls == [(default, "m3-content", False)]


@pytest.mark.parametrize("pure_black", [False, True])
def test_generation_passes_the_shells_pure_black_setting(
    pure_black: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default, calls = _mock_generation(tmp_path, monkeypatch)
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text(f"[theme]\npure_black_dark = {str(pure_black).lower()}\n", encoding="utf-8")

    assert source.from_current_wallpaper() is not None
    assert calls == [(default, "m3-content", pure_black)]


def test_high_contrast_declines_unsupported_generation_and_explains_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, calls = _mock_generation(tmp_path, monkeypatch)
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_text("[accessibility]\nhigh_contrast = true\n", encoding="utf-8")

    resolved = source.resolve()

    assert not calls
    assert resolved.origin is source.Origin.FALLBACK
    assert "high-contrast colours require the palette template" in resolved.detail


@pytest.mark.parametrize(
    "document",
    [
        b"invalid toml",
        b"\xff",
        b'theme = "invalid"',
        b'[theme]\npure_black_dark = "false"',
        b"[accessibility]\nhigh_contrast = 0",
    ],
)
def test_unreadable_transform_settings_do_not_silently_disable_them(
    document: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, calls = _mock_generation(tmp_path, monkeypatch)
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_bytes(document)

    resolved = source.resolve()

    assert not calls
    assert resolved.origin is source.Origin.FALLBACK
    assert "settings could not be read for palette generation" in resolved.detail


def test_generation_settings_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, calls = _mock_generation(tmp_path, monkeypatch)
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b" " * 65)
    monkeypatch.setattr(source, "MAX_SETTINGS_BYTES", 64)

    assert source.from_current_wallpaper() is None
    assert not calls


def test_generated_approximation_keeps_disabled_registration_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_generation(tmp_path, monkeypatch)
    _registration(enabled=False)

    resolved = source.resolve()

    assert resolved.origin is source.Origin.GENERATED
    assert not resolved.is_live
    assert "approximate" in resolved.detail
    assert "template is disabled" in resolved.detail


def test_light_fallback_has_light_surfaces_and_dark_text() -> None:
    light = source.fallback_palette("light")
    dark = source.fallback_palette()
    assert light.mode == "light"
    assert light["surface"].relative_luminance > 0.8
    assert light["on_surface"].relative_luminance < 0.05
    assert dark["surface"].relative_luminance < 0.05
    assert light.colours != dark.colours
    assert light["surface"].hex in css.render(light)


def test_primary_only_template_is_not_a_usable_application_palette(tmp_path: Path) -> None:
    target = tmp_path / "partial.json"
    target.write_text('{"mode":"dark","colors":{"primary":"#fff"}}', encoding="utf-8")
    assert source.from_template(target) is None


@pytest.mark.parametrize("registration", ["missing", "disabled", "misdirected"])
def test_orphaned_template_output_does_not_claim_live_colors(
    registration: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if registration != "missing":
        _registration(
            enabled=registration != "disabled",
            output=tmp_path / "elsewhere.json" if registration == "misdirected" else None,
        )
    _render_palette()
    monkeypatch.setattr(source, "from_current_wallpaper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(noctalia, "current_mode", lambda **_kwargs: "light")
    monkeypatch.setattr(noctalia, "is_available", lambda: True)
    resolved = source.resolve()
    assert resolved.origin is source.Origin.FALLBACK
    assert resolved.palette.mode == "light"
    assert "template" in resolved.detail


def test_registered_rendered_palette_uses_no_shell_ipc(monkeypatch: pytest.MonkeyPatch) -> None:
    _registration()
    _render_palette()
    monkeypatch.setattr(
        source,
        "from_current_wallpaper",
        lambda *_args, **_kwargs: pytest.fail("fresh template must not make shell calls"),
    )
    assert source.template_health() == ""
    assert source.resolve().origin is source.Origin.TEMPLATE


def test_old_render_of_explicit_previous_mode_is_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    _registration()
    _render_palette()
    settings = paths.noctalia_settings_path()
    settings.write_text('[theme]\nmode = "light"\n' + settings.read_text(), encoding="utf-8")
    monkeypatch.setattr(source, "from_current_wallpaper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(noctalia, "current_mode", lambda **_kwargs: "light")
    monkeypatch.setattr(noctalia, "is_available", lambda: True)

    resolved = source.resolve()

    assert resolved.origin is source.Origin.FALLBACK
    assert resolved.palette.mode == "light"
    assert "selected mode" in resolved.detail
