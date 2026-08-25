{
  description = "Wall-in-One - a wallpaper manager for Wayland";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    noctalia-plugins = {
      url = "github:Go08er/goober-noctalia-plugins-v5";
      flake = false;
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      flake-utils,
      noctalia-plugins,
      ...
    }:
    (flake-utils.lib.eachSystem [
      "x86_64-linux"
      "aarch64-linux"
    ] (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        # InterpreterPoolExecutor is the measured isolation boundary between
        # pure-Python provider parsing and GTK.  Pin the interpreter that owns
        # that stdlib API instead of relying on nixpkgs' moving `python3`
        # alias to happen to resolve to 3.14.
        python = pkgs.python314;

        # The GApplication id, and so the Wayland app-id, the desktop entry's
        # filename and the icon's. It is `wall_in_one.paths.APPLICATION_ID`;
        # spelled once here so the three names cannot drift apart.
        applicationId = "dev.goober.WallInOne";

        # Runtime tools bundled with the app. Noctalia remains the selected
        # shell endpoint rather than another copy in this closure: authoring
        # works without it, while runtime status reports application failures.
        runtimeTools = [
          # All-output scenes query the compositor at apply time so hot-plugged
          # connectors never turn into a linux-wallpaperengine preview window.
          pkgs.niri
          pkgs.mpvpaper
          # True Wallpaper Engine scenes and their full-resolution stills.
          # Keeping this in the wrapper PATH makes scene support deterministic
          # instead of depending on whichever ambient PATH launched the app.
          pkgs.linux-wallpaperengine
          # Thumbnails, for stills as well as videos: this closure's GdkPixbuf
          # has no webp or avif loader, and ffmpeg covers every format the
          # library accepts with one code path.
          pkgs.ffmpeg
        ];

        wall-in-one-service = pkgs.rustPlatform.buildRustPackage {
          pname = "wall-in-one-service";
          version = "0.1.0";
          src = pkgs.lib.fileset.toSource {
            root = ./service;
            fileset = pkgs.lib.fileset.unions [
              ./service/Cargo.toml
              ./service/Cargo.lock
              ./service/src
              ./service/tests
            ];
          };
          cargoLock.lockFile = ./service/Cargo.lock;
          doCheck = true;

          # The integration tests write small shell scripts and then have the
          # service exec them. Run in parallel, that is a race the tests cannot
          # win: `fs::write` closes its own handle, but a *different* test thread
          # forking at that instant hands its child a copy of the still-open
          # write descriptor, and Linux refuses to exec a file anybody holds open
          # for writing. It surfaces as "cannot run noctalia: Text file busy
          # (os error 26)" in whichever test happened to be exec'ing, nowhere
          # near the one that caused it, and only under the right scheduling --
          # it survived 37 consecutive runs here and still broke a real rebuild.
          #
          # One thread means no concurrent fork, which removes the race rather
          # than narrowing it. The binary is fourteen tests and under two
          # seconds; running them at once buys nothing worth this.
          RUST_TEST_THREADS = "1";

          meta = with pkgs.lib; {
            description = "Small session runtime for Wall-in-One";
            mainProgram = "wall-in-one-service";
            platforms = platforms.linux;
            license = licenses.mit;
          };
        };

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
              $out/share/systemd/user/wall-in-one.service
            install -Dm644 src/wall_in_one/data/systemd/wall-in-one-health-sync.service \
              $out/share/systemd/user/wall-in-one-health-sync.service
            install -Dm644 src/wall_in_one/data/systemd/wall-in-one-health-sync.timer \
              $out/share/systemd/user/wall-in-one-health-sync.timer
            install -Dm755 ${wall-in-one-service}/bin/wall-in-one-service \
              $out/bin/wall-in-one-service
            substituteInPlace $out/share/applications/${applicationId}.desktop \
              --replace-fail "Exec=wall-in-one" "Exec=$out/bin/wall-in-one"
            substituteInPlace $out/share/systemd/user/wall-in-one.service \
              --replace-fail "ExecStartPre=-wall-in-one" \
              "ExecStartPre=-$out/bin/wall-in-one" \
              --replace-fail "ExecStart=wall-in-one-service" \
              "ExecStart=$out/bin/wall-in-one-service" \
              --replace-fail "ExecStop=-timeout" \
              "ExecStop=-${pkgs.coreutils}/bin/timeout" \
              --replace-fail " wall-in-one --sync-runtime-health-on-stop" \
              " $out/bin/wall-in-one --sync-runtime-health-on-stop"
            substituteInPlace \
              $out/share/systemd/user/wall-in-one-health-sync.service \
              --replace-fail "ExecStart=wall-in-one" \
              "ExecStart=$out/bin/wall-in-one"
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
          inherit wall-in-one wall-in-one-service;
        };

        apps =
          {
            default = flake-utils.lib.mkApp { drv = wall-in-one; } // {
              meta.description = "Open Wall-in-One";
            };
          }
          // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
            vm =
              flake-utils.lib.mkApp {
                drv = self.nixosConfigurations.wall-in-one-vm.config.system.build.vm;
                exePath = "/bin/run-wall-in-one-vm";
              }
              // {
                meta.description = "Launch the isolated Wall-in-One development VM";
              };
          };

        checks = {
          inherit wall-in-one wall-in-one-service;

          # The companion is a separate repository, but this release pins it
          # as part of one runtime contract.  Exercise the exact locked source
          # with Luau available so a future lock update cannot silently skip
          # compilation or ship a status/timeout/health-sync mismatch.
          companion-plugin-contract =
            pkgs.runCommand "wall-in-one-companion-plugin-contract"
              {
                nativeBuildInputs = [
                  python
                  pkgs.luau
                ];
              }
              ''
                cd ${noctalia-plugins}/wall-in-one
                python3 tests/test_thin_client.py
                touch $out
              '';

          # Python and Rust intentionally have different authoring/runtime
          # sockets. This process-level check catches the seam a unit test
          # cannot: with no XDG_RUNTIME_DIR, `wall-in-one ctl status` must find
          # the Rust service at their shared XDG_STATE_HOME fallback.
          runtime-socket-fallback =
            pkgs.runCommand "wall-in-one-runtime-socket-fallback"
              {
                nativeBuildInputs = [
                  (python.withPackages (ps: [ ps.pytest ]))
                ];
              }
              ''
                export HOME="$TMPDIR/home"
                export XDG_CONFIG_HOME="$TMPDIR/config"
                export XDG_STATE_HOME="$TMPDIR/state"
                export XDG_CACHE_HOME="$TMPDIR/cache"
                export XDG_DATA_HOME="$TMPDIR/data"
                unset XDG_RUNTIME_DIR
                export WALL_IN_ONE_SERVICE_BINARY=${wall-in-one-service}/bin/wall-in-one-service
                export WALL_IN_ONE_TEST_TRUE=${pkgs.coreutils}/bin/true
                export WALL_IN_ONE_TEST_FALSE=${pkgs.coreutils}/bin/false
                cd ${./.}
                PYTHONPATH=$PWD/src pytest tests/test_runtime_socket_fallback.py -q \
                  -p no:cacheprovider
                touch $out
              '';

          # Keep the always-on Rust half honest as independent display state
          # grows.  This is a process measurement, not a struct-size estimate:
          # three fresh one-display runs must stay within 5 MiB RSS and three
          # three-display runs within 10 MiB against a 600-item library.  Idle
          # CPU must stay below 2% of one core, while a separate 64-route launch
          # exercises the supported ceiling without turning that synthetic
          # topology into a desktop memory promise.
          service-rss = import ./nix/runtime-rss.nix {
            inherit pkgs;
            wallInOneService = wall-in-one-service;
          };

          # Widget identity, focus, scroll-position and asynchronous GTK
          # delivery regressions need a real display.  Keep those tests out of
          # the package's ordinary checkPhase (which must remain usable in a
          # display-less build sandbox), but do not let that turn `gui` into a
          # marker CI silently never runs.  Xvfb supplies only an isolated X11
          # framebuffer; the tests retain their autouse XDG/network/desktop
          # guards and never see the developer's session.
          gui-tests =
            pkgs.runCommand "wall-in-one-gui-tests"
              {
                nativeBuildInputs = [
                  (python.withPackages (ps: [
                    ps.pygobject3
                    ps.pytest
                  ]))
                  pkgs.adwaita-icon-theme
                  pkgs.glib
                  pkgs.gsettings-desktop-schemas
                  pkgs.gtk4
                  pkgs.libadwaita
                  pkgs.ffmpeg
                  pkgs.xauth
                  pkgs.xvfb-run
                ];
              }
              ''
                export HOME="$TMPDIR/home"
                export XDG_CONFIG_HOME="$TMPDIR/config"
                export XDG_STATE_HOME="$TMPDIR/state"
                export XDG_CACHE_HOME="$TMPDIR/cache"
                export XDG_DATA_HOME="$TMPDIR/data"
                mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" \
                  "$XDG_CACHE_HOME" "$XDG_DATA_HOME"

                export GDK_BACKEND=x11
                export GSETTINGS_BACKEND=memory
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
                }"
                export XDG_DATA_DIRS="${
                  pkgs.lib.concatMapStringsSep ":" (drv: "${drv}/share/gsettings-schemas/${drv.name}") [
                    pkgs.gsettings-desktop-schemas
                    pkgs.gtk4
                  ]
                }:${pkgs.adwaita-icon-theme}/share"

                cd ${./.}
                export PYTHONPATH="$PWD/src"
                xvfb-run --auto-servernum \
                  --server-args='-screen 0 1280x1024x24 -nolisten tcp' \
                  pytest tests -q -m gui -ra -p no:cacheprovider \
                    --junitxml="$TMPDIR/gui-results.xml"

                # importorskip/"no display" are useful for an ad-hoc headless
                # developer run but would make this dedicated display-backed
                # gate a false green.  The report also proves collection did
                # not quietly fall to zero.
                grep -Eq 'tests="[1-9][0-9]*"' "$TMPDIR/gui-results.xml"
                if grep -Eq 'skipped="[1-9][0-9]*"' "$TMPDIR/gui-results.xml"; then
                  echo "the display-backed GUI suite skipped tests" >&2
                  cat "$TMPDIR/gui-results.xml" >&2
                  exit 1
                fi
                touch $out
              '';

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
          # desktop-entry spec, that the icon really rasterises at both the
          # size a panel asks for and the size a settings page does. Both run
          # against the installed paths. The service unit is checked here too:
          # a profile only exposes XDG's share/systemd/user path, not lib, and
          # the unit must name the packaged Rust binary. Its source syntax and
          # lifetime fields are parsed by tests/test_packaging.py; running
          # systemd-analyze in a Nix sandbox is not viable because it insists
          # on creating host /run/systemd state. A broken postInstall fails here
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
                unit=${wall-in-one}/share/systemd/user/wall-in-one.service
                health=${wall-in-one}/share/systemd/user/wall-in-one-health-sync.service
                timer=${wall-in-one}/share/systemd/user/wall-in-one-health-sync.timer
                grep -F 'ExecStartPre=-${wall-in-one}/bin/wall-in-one --write-config' "$unit"
                grep -F 'ExecStart=${wall-in-one}/bin/wall-in-one-service --wait-for-config' "$unit"
                grep -F 'ExecStop=-${pkgs.coreutils}/bin/timeout --signal=TERM --kill-after=0.1s 2s ${wall-in-one}/bin/wall-in-one --sync-runtime-health-on-stop' "$unit"
                # Exercise the exact GNU timeout interval syntax used by the
                # installed ExecStop. Coreutils accepts decimal seconds, not
                # millisecond suffixes such as 750ms.
                ${pkgs.coreutils}/bin/timeout --signal=TERM --kill-after=0.1s 2s ${pkgs.coreutils}/bin/true
                grep -F 'Wants=wall-in-one-health-sync.timer' "$unit"
                grep -F 'ExecStart=${wall-in-one}/bin/wall-in-one --sync-runtime-health' "$health"
                grep -F 'StandardOutput=null' "$health"
                grep -F 'OnUnitInactiveSec=30s' "$timer"
                grep -F 'Persistent=false' "$timer"
                grep -F 'BindsTo=wall-in-one.service' "$timer"
                test -x ${wall-in-one}/bin/wall-in-one-service
                # Exercise the installed Python wrapper as a program, not only
                # its executable bit.  An interpreter/wrapper closure mistake
                # must fail the fast package check rather than waiting for the
                # weekly desktop VM.
                export HOME="$TMPDIR/home"
                export XDG_CONFIG_HOME="$TMPDIR/config"
                export XDG_STATE_HOME="$TMPDIR/state"
                export XDG_CACHE_HOME="$TMPDIR/cache"
                export XDG_DATA_HOME="$TMPDIR/data"
                export XDG_RUNTIME_DIR="$TMPDIR/runtime"
                mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" \
                  "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"
                ${wall-in-one}/bin/wall-in-one --help >/dev/null
                ${wall-in-one}/bin/wall-in-one --version | grep -F 'wall-in-one'
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
        }
        // pkgs.lib.optionalAttrs (system == "x86_64-linux") {
          vm-test = import ./nix/vm-test.nix {
            inherit pkgs;
            wallInOnePackage = wall-in-one;
            pluginSource = noctalia-plugins;
            sampleMedia = import ./nix/sample-media.nix { inherit pkgs; };
          };
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
            pkgs.cargo
            pkgs.clippy
            pkgs.rustc
            pkgs.rustfmt
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
    ))
    // {
      nixosConfigurations.wall-in-one-vm = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        specialArgs = {
          wallInOnePackage = self.packages.x86_64-linux.wall-in-one;
          pluginSource = noctalia-plugins;
          # Only the automated test instruments Noctalia. The interactive VM
          # runs the unwrapped package and therefore receives an explicit null
          # module argument rather than relying on module-argument defaults.
          noctaliaProbe = null;
          sampleMedia = import ./nix/sample-media.nix {
            pkgs = import nixpkgs { system = "x86_64-linux"; };
          };
        };
        modules = [ ./nix/vm.nix ];
      };
    };
}
