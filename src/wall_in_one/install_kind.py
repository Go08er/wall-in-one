"""What kind of install this build is, fixed when a package is built.

``nix`` for this flake's packages, ``system`` for a distribution package built
from source (the Arch package), ``source`` for anything else: a checkout, a
pip install, a test run. A package sets it with ``tools/set-install-kind.sh``
before it builds, which rewrites the one ``INSTALL_KIND`` line below and
nothing else (the flake's ``postPatch``, a PKGBUILD's ``prepare()``). The
service takes the same value from ``WALL_IN_ONE_INSTALL_KIND`` when it is
compiled, and ``tools/check-install-kind.sh`` checks that the two halves of one
package agree. See docs/packaging.md.

It is a line of source rather than an environment variable the launcher
exports, so the installed module carries it whatever imports it: the console
script, ``python -m``, the package's own tests, or a subinterpreter that starts
from a clean environment.
"""

from __future__ import annotations

from typing import Final, Literal

type InstallKind = Literal["nix", "system", "source"]

#: Every install kind, in the order docs/packaging.md lists them.
INSTALL_KINDS: Final[tuple[InstallKind, ...]] = ("nix", "system", "source")

# tools/set-install-kind.sh rewrites the next line; keep it on one line, in
# exactly this shape.
INSTALL_KIND: Final[InstallKind] = "source"
