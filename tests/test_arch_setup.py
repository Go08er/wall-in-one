"""packaging/arch/arch-setup.sh, run against stand-ins for Arch's tools.

The script is for Arch, and these tests run anywhere: pacman, makepkg, curl,
systemctl, the AUR helpers and the installed wall-in-one are small scripts on
a PATH of their own that log how they were called. Only the plain tools the
script uses (bash, sed, grep, ...) are the real ones, linked into that PATH,
so nothing installed on the machine running the tests is seen. The tests run
it with WIO_SETUP_NO_TTY_OK=1, the seam that lets it run without a terminal.
"""

import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "arch" / "arch-setup.sh"
PKGBUILD = ROOT / "packaging" / "arch" / "PKGBUILD"
REAL_TOOLS = (
    "bash",
    "sed",
    "grep",
    "mktemp",
    "mkdir",
    "rm",
    "cp",
    "cat",
    "id",
    "tr",
    "printf",
    "sort",
    "head",
)

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="no arch-setup.sh in this tree")


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "$SETUP_LOG"\n{body}\n')
    path.chmod(0o755)


def _released_pkgbuild(tmp_path: Path) -> Path:
    """The PKGBUILD a release carries, made the way releases make it (aur-release.sh)."""
    found = re.search(r"^pkgver=(\S+)$", PKGBUILD.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None
    version = found.group(1)
    tarball = tmp_path / "source.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        member = tarfile.TarInfo(f"wall-in-one-{version}/LICENSE")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    out = tmp_path / "release"
    subprocess.run(
        ["bash", str(PKGBUILD.parent / "aur-release.sh"), f"v{version}", str(out)],
        check=True,
        capture_output=True,
        env={**os.environ, "WIO_AUR_TARBALL": str(tarball), "TMPDIR": str(tmp_path)},
    )
    released = out / "PKGBUILD"
    assert "SKIP" not in released.read_text(encoding="utf-8")
    return released


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.cache = tmp_path / "cache"
        self.config = tmp_path / "config"
        self.system_conf = tmp_path / "etc-paru.conf"
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.served = _released_pkgbuild(tmp_path)
        self.pkgver_rel = _pkgver_rel(self.served)
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (self.bin / tool).symlink_to(found)
        self.installed(None)
        _stub(self.bin, "sudo", "")
        # vercmp prints -1, 0 or 1 like pacman's. It models what the tests need and no
        # more: an epoch (default 0) decides first, as a number, then sort -V orders
        # the rest. pacman's own ordering has more rules (alpha/beta, pkgrel).
        _stub(
            self.bin,
            "vercmp",
            'if [ "$1" = "$2" ]; then echo 0; exit 0; fi\n'
            "e1=0; e2=0; r1=$1; r2=$2\n"
            'case "$1" in *:*) e1=${1%%:*}; r1=${1#*:} ;; esac\n'
            'case "$2" in *:*) e2=${2%%:*}; r2=${2#*:} ;; esac\n'
            '[ "$e1" -lt "$e2" ] && { echo -1; exit 0; }\n'
            '[ "$e1" -gt "$e2" ] && { echo 1; exit 0; }\n'
            'first=$(printf "%s\\n%s\\n" "$r1" "$r2" | sort -V | head -n 1)\n'
            'if [ "$r1" = "$r2" ]; then echo 0\n'
            'elif [ "$first" = "$r1" ]; then echo -1; else echo 1; fi',
        )
        # curl -fsSL --proto '=https' -o FILE URL: serve the release PKGBUILD.
        _stub(
            self.bin,
            "curl",
            'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'cp "$SERVED" "$out"',
        )
        _stub(self.bin, "makepkg", "[ -f PKGBUILD ] || exit 9")
        _stub(
            self.bin,
            "wall-in-one",
            'case "$1" in --version) printf "wall-in-one 0.4.0\\ninstall kind: system\\n" ;;'
            " --setup-status) exit 1 ;; esac",
        )
        self.systemctl(env="WAYLAND_DISPLAY=wayland-0")

    def installed(self, version: str | None) -> None:
        """What `pacman -Q wall-in-one` answers: a version-release, or not installed."""
        answer = f'echo "wall-in-one {version}"' if version else "exit 1"
        _stub(
            self.bin,
            "pacman",
            f'case "$1 $2" in "-Q wall-in-one") {answer} ;; "-Q base-devel") exit 0 ;; esac',
        )

    def systemctl(self, env: str = "", active: bool = False, start_fails: bool = False) -> None:
        """A user manager whose environment is `env`, with the unit running or not.

        `start_fails` is a first install: the unit's preparation refuses (exit 78)
        until the app has saved its settings.
        """
        _stub(
            self.bin,
            "systemctl",
            f'case "$*" in "--user show-environment") printf "PATH=/usr/bin\\n{env}\\n" ;;'
            f' "--user is-active --quiet wall-in-one.service") exit {0 if active else 3} ;;'
            f' "--user start wall-in-one.service") exit {1 if start_fails else 0} ;; esac',
        )

    def run(
        self, *args: str, tty_seam: bool = True, **env: str | None
    ) -> subprocess.CompletedProcess[str]:
        environment: dict[str, str | None] = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp / "home"),
            "TMPDIR": str(self.tmp),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_CONFIG_HOME": str(self.config),
            "WIO_SETUP_PARU_SYSTEM_CONF": str(self.system_conf),
            "SETUP_LOG": str(self.log),
            "SERVED": str(self.served),
            "WIO_SETUP_NIX_DIRS": "",
            "WIO_SETUP_NO_TTY_OK": "1" if tty_seam else None,
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={key: value for key, value in environment.items() if value is not None},
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def made(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls())


def _pkgver_rel(pkgbuild: Path) -> str:
    text = pkgbuild.read_text(encoding="utf-8")
    ver = re.search(r"^pkgver=(\S+)$", text, re.MULTILINE)
    rel = re.search(r"^pkgrel=(\S+)$", text, re.MULTILINE)
    assert ver is not None
    assert rel is not None
    return f"{ver.group(1)}-{rel.group(1)}"


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_it_builds_the_latest_release_and_enables_the_service(setup: Setup) -> None:
    _stub(setup.bin, "paru", "")
    _stub(setup.bin, "yay", "")
    result = setup.run()
    assert result.returncode == 0, result.stderr
    calls = setup.calls()
    assert any(
        call.endswith("https://github.com/Go08er/wall-in-one/releases/latest/download/PKGBUILD")
        for call in calls
        if call.startswith("curl ")
    ), calls
    assert "makepkg -si" in calls
    assert "systemctl --user enable wall-in-one.service" in calls
    assert "systemctl --user start wall-in-one.service" in calls
    assert "wall-in-one --setup-status" in calls
    # Without a terminal to ask on, no AUR helper is run, though two are there,
    # and paru's config is not touched.
    assert not any(call.startswith(("paru ", "yay ")) for call in calls)
    assert not setup.config.exists()
    assert "videos and scenes show their stills" in result.stdout
    # Its build directory is gone afterwards.
    assert not list(setup.cache.glob("wall-in-one-setup.*"))


def test_a_version_downloads_that_release(setup: Setup) -> None:
    result = setup.run("--version", "v0.4.0")
    assert result.returncode == 0, result.stderr
    assert any(
        call.endswith("/releases/download/v0.4.0/PKGBUILD")
        for call in setup.calls()
        if call.startswith("curl ")
    )


def test_renderers_come_from_the_aur_helper_when_asked(setup: Setup) -> None:
    _stub(setup.bin, "paru", "")
    _stub(setup.bin, "yay", "")
    result = setup.run("--renderers")
    assert result.returncode == 0, result.stderr
    assert "paru -S --needed mpvpaper linux-wallpaperengine-git" in setup.calls()
    assert not any(call.startswith("yay ") for call in setup.calls())


def test_installed_renderers_are_not_asked_for(setup: Setup) -> None:
    _stub(setup.bin, "paru", "")
    _stub(setup.bin, "mpvpaper", "")
    result = setup.run("--renderers")
    assert result.returncode == 0, result.stderr
    assert "paru -S --needed linux-wallpaperengine-git" in setup.calls()


def test_no_enable_leaves_the_service_alone(setup: Setup) -> None:
    result = setup.run("--no-enable", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("systemctl ") for call in setup.calls())


def test_the_session_display_is_given_to_the_user_manager(setup: Setup) -> None:
    setup.systemctl(env="")
    result = setup.run("--no-renderers", WAYLAND_DISPLAY="wayland-1")
    assert result.returncode == 0, result.stderr
    calls = setup.calls()
    assert "systemctl --user import-environment WAYLAND_DISPLAY" in calls
    assert calls.index("systemctl --user import-environment WAYLAND_DISPLAY") < calls.index(
        "systemctl --user start wall-in-one.service"
    )


def test_no_user_manager_means_no_systemctl_enable(setup: Setup) -> None:
    _stub(setup.bin, "systemctl", "exit 1")
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    assert not setup.made("systemctl --user enable")
    assert not setup.made("systemctl --user start")
    assert "companion plugin starts the service directly" in result.stdout


def test_a_nix_install_stops_it_before_anything_is_built(setup: Setup) -> None:
    nix_bin = setup.tmp / "nix-profile-bin"
    nix_bin.mkdir()
    (nix_bin / "wall-in-one").write_text("#!/bin/sh\n")
    (nix_bin / "wall-in-one").chmod(0o755)
    result = setup.run(WIO_SETUP_NIX_DIRS=str(nix_bin))
    assert result.returncode == 1
    assert "already installed with Nix" in result.stderr
    assert not any(call.startswith(("curl ", "makepkg ")) for call in setup.calls())


def test_a_pkgbuild_without_a_checksum_is_refused(setup: Setup) -> None:
    setup.served.write_text(PKGBUILD.read_text(encoding="utf-8"))  # still says SKIP
    result = setup.run()
    assert result.returncode == 1
    assert "no checksum" in result.stderr
    assert not any(call.startswith("makepkg ") for call in setup.calls())


def test_something_else_than_our_pkgbuild_is_refused(setup: Setup) -> None:
    setup.served.write_text("<html>not found</html>\n")
    result = setup.run()
    assert result.returncode == 1
    assert "not Wall-in-One's PKGBUILD" in result.stderr


def test_base_devel_is_installed_when_missing(setup: Setup) -> None:
    _stub(setup.bin, "pacman", 'case "$1" in -Q) exit 1 ;; esac')
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "sudo pacman -S --needed base-devel" in setup.calls()


def test_without_pacman_it_says_it_is_for_arch(setup: Setup) -> None:
    (setup.bin / "pacman").unlink()
    result = setup.run()
    assert result.returncode == 1
    assert "this is for Arch Linux" in result.stderr


@pytest.mark.skipif(os.getuid() == 0, reason="root is what it refuses")
def test_an_unknown_option_is_refused(setup: Setup) -> None:
    result = setup.run("--bogus")
    assert result.returncode == 1
    assert "unknown option" in result.stderr


def test_help_prints_the_usage(setup: Setup) -> None:
    result = setup.run("--help")
    assert result.returncode == 0
    assert "arch-setup.sh [--version X.Y.Z]" in result.stdout
    assert "set -euo" not in result.stdout


def test_a_failed_download_stops_before_makepkg(setup: Setup) -> None:
    _stub(setup.bin, "curl", "exit 22")
    result = setup.run("--no-renderers")
    assert result.returncode == 1
    assert "could not download the PKGBUILD" in result.stderr
    assert not setup.made("makepkg ")
    assert not setup.made("systemctl ")
    # Nothing was built, so nothing is kept.
    assert not list(setup.cache.glob("wall-in-one-setup.*"))


def test_a_failed_build_keeps_its_files_and_enables_nothing(setup: Setup) -> None:
    _stub(setup.bin, "makepkg", "exit 1")
    result = setup.run("--no-renderers")
    assert result.returncode != 0
    assert not setup.made("systemctl --user enable")
    assert not setup.made("wall-in-one --setup-status")
    kept = list(setup.cache.glob("wall-in-one-setup.*"))
    assert len(kept) == 1
    assert (kept[0] / "PKGBUILD").is_file()
    assert f"the build files are kept in {kept[0]}" in result.stderr


def test_the_build_goes_in_the_cache_directory(setup: Setup) -> None:
    _stub(setup.bin, "makepkg", "exit 1")
    setup.run("--no-renderers", XDG_CACHE_HOME=str(setup.tmp / "elsewhere"))
    assert len(list((setup.tmp / "elsewhere").glob("wall-in-one-setup.*"))) == 1
    assert not list(setup.tmp.glob("wall-in-one-setup.*"))


def test_root_is_refused(setup: Setup) -> None:
    (setup.bin / "id").unlink()
    _stub(setup.bin, "id", 'case "$1" in -u) echo 0 ;; *) echo root ;; esac')
    result = setup.run()
    assert result.returncode == 1
    assert "not as root" in result.stderr
    assert not setup.made("curl ")


def test_a_piped_run_is_refused_without_the_test_seam(setup: Setup) -> None:
    result = setup.run(tty_seam=False)
    assert result.returncode == 1
    assert "from a terminal, as a file" in result.stderr
    assert not setup.made("curl ")


def test_missing_sudo_is_said_before_anything_is_downloaded(setup: Setup) -> None:
    (setup.bin / "sudo").unlink()
    result = setup.run()
    assert result.returncode == 1
    assert "sudo is not installed" in result.stderr
    assert not setup.made("curl ")


def test_a_manager_that_has_the_display_is_not_given_it_again(setup: Setup) -> None:
    setup.systemctl(env="WAYLAND_DISPLAY=wayland-0")
    result = setup.run("--no-renderers", WAYLAND_DISPLAY="wayland-1")
    assert result.returncode == 0, result.stderr
    assert not setup.made("systemctl --user import-environment")
    assert "systemctl --user start wall-in-one.service" in setup.calls()


def test_without_any_display_the_unit_is_enabled_but_not_started(setup: Setup) -> None:
    setup.systemctl(env="")
    result = setup.run("--no-renderers", WAYLAND_DISPLAY=None)
    assert result.returncode == 0, result.stderr
    calls = setup.calls()
    assert "systemctl --user enable wall-in-one.service" in calls
    assert not any("--now" in call for call in calls)
    assert not setup.made("systemctl --user import-environment")
    assert "docs/installing.md#the-service-needs-your-sessions-display" in result.stdout


def test_an_update_reloads_the_units_and_restarts_the_running_service(setup: Setup) -> None:
    setup.installed("0.3.0-1")
    setup.systemctl(env="WAYLAND_DISPLAY=wayland-0", active=True)
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    calls = setup.calls()
    assert "makepkg -si" in calls
    assert (
        calls.index("makepkg -si")
        < calls.index("systemctl --user daemon-reload")
        < calls.index("systemctl --user restart wall-in-one.service")
    )
    assert "systemctl --user enable wall-in-one.service" in calls
    assert not any("--now" in call for call in calls)


def test_an_up_to_date_install_is_not_rebuilt_but_still_set_up(setup: Setup) -> None:
    setup.installed(setup.pkgver_rel)
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    assert not setup.made("makepkg ")
    assert "which is this release" in result.stdout
    calls = setup.calls()
    assert "systemctl --user enable wall-in-one.service" in calls
    assert "systemctl --user start wall-in-one.service" in calls
    assert "wall-in-one --setup-status" in calls
    assert not setup.made("systemctl --user restart")


def test_a_newer_install_is_not_downgraded(setup: Setup) -> None:
    setup.installed("99.0.0-1")
    result = setup.run("--no-renderers")
    assert result.returncode == 1
    assert "newer than" in result.stderr
    assert not setup.made("makepkg ")
    assert not setup.made("systemctl --user enable")


def test_a_release_that_is_not_the_one_asked_for_is_refused(setup: Setup) -> None:
    result = setup.run("--version", "0.3.0", "--no-renderers")
    assert result.returncode == 1
    assert "says it is" in result.stderr
    assert not setup.made("makepkg ")


def test_a_failing_helper_does_not_stop_the_setup(setup: Setup) -> None:
    _stub(setup.bin, "paru", "exit 1")
    result = setup.run("--renderers")
    assert result.returncode == 0, result.stderr
    assert "paru could not install" in result.stdout
    assert "systemctl --user start wall-in-one.service" in setup.calls()
    assert "wall-in-one --setup-status" in setup.calls()


def test_the_default_nix_directories_are_checked(setup: Setup) -> None:
    profile = setup.tmp / "home" / ".nix-profile" / "bin"
    profile.mkdir(parents=True)
    (profile / "wall-in-one").write_text("#!/bin/sh\n")
    (profile / "wall-in-one").chmod(0o755)
    result = setup.run(WIO_SETUP_NIX_DIRS=None)
    assert result.returncode == 1
    assert "already installed with Nix" in result.stderr
    assert not setup.made("curl ")


SECTION = (
    "[wall-in-one]\nUrl = https://github.com/Go08er/wall-in-one\nPath = packaging/arch/release\n"
)


def _paru(setup: Setup, system: str | None = "[options]\nBottomUp\n") -> Path:
    """paru is installed, its system config says `system`; returns the user config's path."""
    _stub(setup.bin, "paru", "")
    if system is not None:
        setup.system_conf.write_text(system, encoding="utf-8")
    return setup.config / "paru" / "paru.conf"


def test_paru_installs_it_and_the_new_config_keeps_the_system_one(setup: Setup) -> None:
    conf = _paru(setup)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    # The user config replaces /etc/paru.conf, so it includes it, then adds the section.
    assert conf.read_text(encoding="utf-8") == f"Include = {setup.system_conf}\n\n{SECTION}"
    assert f"created {conf}" in result.stdout
    calls = setup.calls()
    assert calls.index("paru -Sy --pkgbuilds") < calls.index("paru -S wall-in-one/wall-in-one")
    assert not setup.made("makepkg ")
    assert "systemctl --user enable wall-in-one.service" in calls
    assert "systemctl --user start wall-in-one.service" in calls


def test_a_new_paru_config_without_a_system_one_is_just_the_section(setup: Setup) -> None:
    conf = _paru(setup, system=None)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert conf.read_text(encoding="utf-8") == SECTION


def test_an_existing_paru_config_only_gets_the_section_appended(setup: Setup) -> None:
    conf = _paru(setup)
    conf.parent.mkdir(parents=True)
    mine = "[options]\nSudoLoop\n\n[bin]\nFileManager = vifm"  # no final newline
    conf.write_text(mine, encoding="utf-8")
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    text = conf.read_text(encoding="utf-8")
    assert text.startswith(mine)
    assert text[len(mine) :].strip() == SECTION.strip()
    assert "Include" not in text
    assert f"added a [wall-in-one] section to {conf}" in result.stdout


def test_an_existing_wall_in_one_section_is_left_alone(setup: Setup) -> None:
    conf = _paru(setup)
    conf.parent.mkdir(parents=True)
    mine = "[wall-in-one]\nUrl = file:///home/me/wall-in-one\n"
    conf.write_text(mine, encoding="utf-8")
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert conf.read_text(encoding="utf-8") == mine
    assert "has a [wall-in-one] section already" in result.stdout
    assert setup.made("paru -S wall-in-one/wall-in-one")


def test_a_system_section_means_no_user_config_is_made(setup: Setup) -> None:
    conf = _paru(setup, system="[options]\n\n" + SECTION)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert not conf.exists()


def test_paru_conf_names_the_file_to_change(setup: Setup) -> None:
    _paru(setup)
    mine = setup.tmp / "mine.conf"
    mine.write_text("[options]\n", encoding="utf-8")
    result = setup.run("--paru", "--no-renderers", PARU_CONF=str(mine))
    assert result.returncode == 0, result.stderr
    assert mine.read_text(encoding="utf-8").endswith(SECTION)
    assert not setup.config.exists()


def test_a_missing_paru_conf_file_is_refused(setup: Setup) -> None:
    _paru(setup)
    result = setup.run("--paru", "--no-renderers", PARU_CONF=str(setup.tmp / "nope.conf"))
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not setup.made("paru -S ")


def test_a_relative_config_home_is_ignored_like_paru_does(setup: Setup) -> None:
    conf = _paru(setup)
    result = setup.run("--paru", "--no-renderers", XDG_CONFIG_HOME="relative")
    assert result.returncode == 0, result.stderr
    assert (setup.tmp / "home" / ".config" / "paru" / "paru.conf").is_file()
    assert not conf.exists()


def test_no_paru_leaves_paru_alone(setup: Setup) -> None:
    _paru(setup)
    result = setup.run("--no-paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert not setup.made("paru ")
    assert setup.made("makepkg ")
    assert not setup.config.exists()


def test_paru_needs_paru_and_the_latest_release(setup: Setup) -> None:
    result = setup.run("--paru")
    assert result.returncode == 1
    assert "needs paru" in result.stderr
    _paru(setup)
    result = setup.run("--paru", "--version", "0.4.0")
    assert result.returncode == 1
    assert "follows the latest release" in result.stderr
    assert not setup.made("curl ")


def test_a_paru_failure_stops_before_the_service(setup: Setup) -> None:
    _paru(setup)
    _stub(setup.bin, "paru", 'case "$1" in -S) exit 1 ;; esac')
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 1
    assert "--no-paru" in result.stderr
    assert not setup.made("systemctl --user enable")


def test_an_up_to_date_install_is_still_registered_with_paru(setup: Setup) -> None:
    conf = _paru(setup)
    setup.installed(setup.pkgver_rel)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert conf.read_text(encoding="utf-8").endswith(SECTION)
    assert not setup.made("paru -S")
    assert not setup.made("makepkg ")


def test_the_download_only_follows_https_redirects(setup: Setup) -> None:
    setup.run("--no-renderers")
    curl = next(call for call in setup.calls() if call.startswith("curl "))
    assert "--proto =https --proto-redir =https" in curl


def test_an_empty_paru_conf_is_set_and_missing(setup: Setup) -> None:
    _paru(setup)
    result = setup.run("--paru", "--no-renderers", PARU_CONF="")
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not setup.made("paru -S")
    assert not setup.config.exists()


def _user_conf(setup: Setup, text: str) -> Path:
    conf = _paru(setup)
    conf.parent.mkdir(parents=True)
    conf.write_text(text, encoding="utf-8")
    return conf


def test_an_existing_section_is_shown_and_a_good_one_has_no_warning(setup: Setup) -> None:
    _user_conf(setup, SECTION)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "Url = https://github.com/Go08er/wall-in-one, Path = packaging/arch/release" in (
        result.stdout
    )
    assert "WARNING" not in result.stdout


def test_a_stale_or_forked_section_is_warned_about(setup: Setup) -> None:
    stale = "[wall-in-one]\nUrl=https://github.com/someone/fork\nPath = packaging/arch\n"
    conf = _user_conf(setup, stale)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "Url = https://github.com/someone/fork, Path = packaging/arch" in result.stdout
    assert "WARNING: expected Url = https://github.com/Go08er/wall-in-one" in result.stdout
    assert conf.read_text(encoding="utf-8") == stale


def test_a_section_in_another_case_is_reported_and_not_duplicated(setup: Setup) -> None:
    mine = "[Wall-In-One]\nUrl = https://github.com/Go08er/wall-in-one\n"
    conf = _user_conf(setup, mine)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert conf.read_text(encoding="utf-8") == mine
    assert "spelled [Wall-In-One]" in result.stdout


def test_a_section_in_an_included_file_is_seen(setup: Setup) -> None:
    extra = setup.tmp / "extra.conf"
    extra.write_text(SECTION, encoding="utf-8")
    mine = f"Include = {extra}\n"
    conf = _user_conf(setup, mine)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert conf.read_text(encoding="utf-8") == mine
    assert f"{extra} has a [wall-in-one] section" in result.stdout


def _serve(setup: Setup, *edits: tuple[str, str]) -> None:
    text = setup.served.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    setup.served.write_text(text, encoding="utf-8")


def test_quoted_versions_are_compared_unquoted(setup: Setup) -> None:
    ver, rel = setup.pkgver_rel.split("-")
    _serve(setup, (f"pkgver={ver}", f'pkgver="{ver}"'), (f"pkgrel={rel}", f"pkgrel='{rel}'"))
    setup.installed(setup.pkgver_rel)
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "which is this release" in result.stdout
    assert not setup.made("makepkg ")


def test_an_epoch_is_part_of_the_version(setup: Setup) -> None:
    ver, _rel = setup.pkgver_rel.split("-")
    _serve(setup, (f"pkgver={ver}", f"epoch=1\npkgver={ver}"))
    # Installed 1:ver-rel is this release; 0:99-1 is older than any epoch 1.
    setup.installed(f"1:{setup.pkgver_rel}")
    assert "which is this release" in setup.run("--no-renderers").stdout
    setup.installed("99.0.0-1")
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    assert setup.made("makepkg ")


def test_an_epoch_installed_beats_a_release_without_one(setup: Setup) -> None:
    setup.installed(f"1:{setup.pkgver_rel}")
    result = setup.run("--no-renderers")
    assert result.returncode == 1
    assert "newer than" in result.stderr


def _paru_that_installs(setup: Setup, installs: bool) -> None:
    pacman = setup.bin / "pacman"
    change = (
        f'sed -i "s/wall-in-one 0.3.0-1/wall-in-one {setup.pkgver_rel}/" {pacman}'
        if installs
        else ""
    )
    _stub(setup.bin, "paru", f'case "$1" in -S) {change or ":"} ;; esac')


def test_paru_that_installs_restarts_the_running_service(setup: Setup) -> None:
    _paru(setup)
    _paru_that_installs(setup, installs=True)
    setup.installed("0.3.0-1")
    setup.systemctl(env="WAYLAND_DISPLAY=wayland-0", active=True)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "systemctl --user restart wall-in-one.service" in setup.calls()


def test_paru_with_nothing_to_do_does_not_restart_the_service(setup: Setup) -> None:
    _paru(setup)
    _paru_that_installs(setup, installs=False)
    setup.installed("0.3.0-1")
    setup.systemctl(env="WAYLAND_DISPLAY=wayland-0", active=True)
    result = setup.run("--paru", "--no-renderers")
    assert result.returncode == 0, result.stderr
    assert "paru had nothing to install" in result.stdout
    assert not setup.made("systemctl --user restart")


def test_a_first_install_without_settings_is_enabled_and_explained(setup: Setup) -> None:
    """On real Arch the first start fails until the app has a library folder (exit 78)."""
    setup.systemctl(env="WAYLAND_DISPLAY=wayland-0", start_fails=True)
    result = setup.run("--no-renderers")
    assert result.returncode == 0, result.stderr
    calls = setup.calls()
    assert "systemctl --user enable wall-in-one.service" in calls
    assert "systemctl --user start wall-in-one.service" in calls
    assert "open Wall-in-One, choose your library folder" in result.stdout
    assert "wall-in-one --setup-status" in calls
