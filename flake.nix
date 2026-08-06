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
          nativeCheckInputs = [ python.pkgs.pytest ];
          checkPhase = ''
            runHook preCheck
            PYTHONPATH=$PWD/src:$PYTHONPATH pytest tests -q -m "not gui"
            runHook postCheck
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

          ruff =
            pkgs.runCommand "wall-in-one-ruff" { nativeBuildInputs = [ pkgs.ruff ]; }
              ''
                cd ${./.}
                ruff check src tests
                ruff format --check src tests
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
