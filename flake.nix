{
  description = "Wall-in-One - a wallpaper manager for Wayland";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python3;

        # The GApplication id, and so the Wayland app-id, the desktop entry's
        # filename and the icon's. It is `wall_in_one.paths.APPLICATION_ID`;
        # spelled once here so the three names cannot drift apart.
        applicationId = "dev.goober.WallInOne";

        # Runtime tools the app shells out to. noctalia is deliberately *not*
        # here: the app degrades gracefully without it, and hard-depending on
        # it would force a shell install on someone who only wants the manager.
        runtimeTools = [
          pkgs.mpvpaper
          # Thumbnails, for stills as well as videos: this closure's GdkPixbuf
          # has no webp or avif loader, and ffmpeg covers every format the
          # library accepts with one code path.
          pkgs.ffmpeg
        ];

        wall-in-one = python.pkgs.buildPythonApplication {
          pname = "wall-in-one";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ python.pkgs.setuptools ];

          dependencies = [ python.pkgs.pygobject3 ];

          nativeBuildInputs = [
            pkgs.wrapGAppsHook4
            pkgs.gobject-introspection
          ];

          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.glib
            # GTK reads gtk-application-prefer-dark-theme and friends from
            # GSettings; without the schemas on XDG_DATA_DIRS it warns on every
            # start. wrapGAppsHook4 puts them there.
            pkgs.gsettings-desktop-schemas
          ];

          # The GUI needs a display; the offline suite is what CI can run.
          #
          # ffmpeg is here as well as in `runtimeTools` because the runtime
          # wrapper does not exist yet during the check phase: without it the
          # thumbnail and still-generation tests either skip or, worse, pass
          # for the wrong reason -- `test_a_missing_file_is_reported` was
          # getting "ffmpeg is not installed" and matching nothing it meant to.
          # Twenty-nine tests were silently sitting out the packaged build.
          nativeCheckInputs = [ python.pkgs.pytest ] ++ runtimeTools;
          checkPhase = ''
            runHook preCheck
            PYTHONPATH=$PWD/src:$PYTHONPATH pytest tests -q -m "not gui"
            runHook postCheck
          '';

          # The launcher entry and its icon go where XDG looks for them, so
          # that `nix profile install` produces something a menu can find. They
          # travel in the wheel too (package-data), but site-packages is not a
          # place any desktop shell reads.
          #
          # Exec is rewritten from the bare command to this store path because
          # a session whose PATH never picked up the profile would otherwise
          # own a menu entry it cannot start. The same substitution catches
          # TryExec, which holds the same string.
          postInstall = ''
            install -Dm644 src/wall_in_one/data/${applicationId}.desktop \
              $out/share/applications/${applicationId}.desktop
            install -Dm644 src/wall_in_one/data/${applicationId}.svg \
              $out/share/icons/hicolor/scalable/apps/${applicationId}.svg
            install -Dm644 src/wall_in_one/data/systemd/wall-in-one.service \
              $out/lib/systemd/user/wall-in-one.service
            substituteInPlace $out/share/applications/${applicationId}.desktop \
              --replace-fail "Exec=wall-in-one" "Exec=$out/bin/wall-in-one"
            substituteInPlace $out/lib/systemd/user/wall-in-one.service \
              --replace-fail "ExecStart=wall-in-one" "ExecStart=$out/bin/wall-in-one"
          '';

          # buildPythonApplication's wrapper and wrapGAppsHook4's wrapper both
          # want to run; this keeps them from wrapping twice.
          dontWrapGApps = true;
          preFixup = ''
            makeWrapperArgs+=("''${gappsWrapperArgs[@]}")
            makeWrapperArgs+=(--prefix PATH : ${pkgs.lib.makeBinPath runtimeTools})
          '';

          meta = with pkgs.lib; {
            description = "A wallpaper manager for Wayland with Noctalia palette sync";
            mainProgram = "wall-in-one";
            platforms = platforms.linux;
            license = licenses.mit;
          };
        };
      in
      {
        packages = {
          default = wall-in-one;
          inherit wall-in-one;
        };

        apps.default = flake-utils.lib.mkApp { drv = wall-in-one; };

        checks = {
          inherit wall-in-one;

          mypy =
            pkgs.runCommand "wall-in-one-mypy"
              {
                nativeBuildInputs = [
                  (python.withPackages (ps: [
                    ps.mypy
                    ps.pygobject3
                    ps.pygobject-stubs
                    ps.pytest
                  ]))
                ];
              }
              ''
                cd ${./.}
                mypy --strict --no-incremental --cache-dir=/dev/null src tests
                touch $out
              '';

          # What the Python tests cannot see: that the entry satisfies the
          # desktop-entry spec, and that the icon really rasterises at both the
          # size a panel asks for and the size a settings page does. Both run
          # against the installed paths, so a broken postInstall fails here
          # rather than on someone's menu. Two tiny tools on top of a package
          # that had to be built anyway.
          desktop =
            pkgs.runCommand "wall-in-one-desktop"
              {
                nativeBuildInputs = [
                  pkgs.desktop-file-utils
                  pkgs.librsvg
                ];
              }
              ''
                desktop-file-validate ${wall-in-one}/share/applications/${applicationId}.desktop
                for size in 16 128; do
                  rsvg-convert -w "$size" -h "$size" \
                    ${wall-in-one}/share/icons/hicolor/scalable/apps/${applicationId}.svg \
                    -o "rendered-$size.png"
                done
                touch $out
              '';

          ruff =
            pkgs.runCommand "wall-in-one-ruff" { nativeBuildInputs = [ pkgs.ruff ]; }
              ''
                cd ${./.}
                # `cd` lands in the read-only store, where ruff cannot create
                # the cache it makes next to the files it is linting. The
                # check is a one-shot in a fresh sandbox, so there is nothing
                # for a cache to make faster anyway -- mypy above is told the
                # same thing in its own spelling.
                export RUFF_CACHE_DIR="$TMPDIR/ruff-cache"
                ruff check --no-cache src tests
                ruff format --check --no-cache src tests
                touch $out
              '';
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: [
              ps.pygobject3
              ps.pygobject-stubs
              ps.pytest
              ps.mypy
            ]))
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.glib
            pkgs.gobject-introspection
            pkgs.gsettings-desktop-schemas
            pkgs.ruff
          ] ++ runtimeTools;

          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            # The packaged build gets these from wrapGAppsHook4; the dev shell
            # has to say it out loud or GTK warns about missing schemas.
            export XDG_DATA_DIRS="${
              pkgs.lib.concatMapStringsSep ":" (drv: "${drv}/share/gsettings-schemas/${drv.name}") [
                pkgs.gsettings-desktop-schemas
                pkgs.gtk4
              ]
            }''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
            export GI_TYPELIB_PATH="${
              pkgs.lib.makeSearchPath "lib/girepository-1.0" [
                pkgs.gtk4
                pkgs.libadwaita
                pkgs.glib.out
                pkgs.gobject-introspection
                pkgs.pango.out
                pkgs.harfbuzz
                pkgs.gdk-pixbuf
                pkgs.graphene
                pkgs.at-spi2-core
              ]
            }''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
            echo "wall-in-one dev shell -- run: python -m wall_in_one"
          '';
        };
      }
    );
}
