{ pkgs, wallInOneService }:

let
  lib = pkgs.lib;
  playlists = builtins.genList (index: index) 4;
  entries = builtins.genList (index: index) 16;
  mkOutputs = count: builtins.genList (index: "OUT-${toString index}") count;

  still = pkgs.writeText "wall-in-one-rss-still.png" "offline fake still\n";
  mkOutputReply = count:
    pkgs.writeText "wall-in-one-rss-${toString count}-outputs.json" (
      builtins.toJSON (
        builtins.listToAttrs (
          map (connector: {
            name = connector;
            value = {
              name = connector;
              current_mode = 0;
              modes = [ { refresh_rate = 60000; } ];
            };
          }) (mkOutputs count)
        )
      )
    );
  mkFakeNiri = count:
    pkgs.writeShellScript "wall-in-one-rss-${toString count}-niri" ''
      if [ "$*" != "msg --json outputs" ]; then
        echo "unexpected fake niri arguments: $*" >&2
        exit 2
      fi
      exec ${pkgs.coreutils}/bin/cat ${mkOutputReply count}
    '';
  fakeNoctalia = pkgs.writeShellScript "wall-in-one-rss-noctalia" ''
    exit 0
  '';

  mkRuntimeConfig = count: pkgs.writeText "wall-in-one-rss-${toString count}-runtime.toml" (
    ''
      schema_version = 4
      config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      default_playlist = "p0"

      [settings]
      cycle_interval_seconds = 300
      cycle_enabled = false
      shuffle = false
      dynamics_enabled = false
      display_mode = "independent"
      theme_source_connector = "OUT-0"

      [renderer]
      noctalia_program = "${fakeNoctalia}"
      niri_program = "${mkFakeNiri count}"
      mpvpaper_program = "${pkgs.coreutils}/bin/false"
      linux_wallpaperengine_program = "${pkgs.coreutils}/bin/false"
      own_scene_renderer = false
      layer = "background"
      video_when_hidden = "pause"
      video_hardware_decode = true
      video_interpolation = "off"
      video_muted = true
      video_volume = 0
      scene_fps = 30
      scene_muted = true
      scene_volume = 0
      scene_pause_when_covered = true
      scene_scaling = ""
      scene_clamp = ""
    ''
    + lib.concatMapStrings (playlist: ''

      [[playlists]]
      id = "p${toString playlist}"
      name = "Playlist ${toString playlist}"
      ${lib.concatMapStrings (entry: ''

      [[playlists.entries]]
      id = "p${toString playlist}-e${toString entry}"
      kind = "still"
      still = "${still}"
      palette = { kind = "keep", mode = "keep" }
      '') entries}
    '') playlists
    + lib.concatMapStrings (connector: ''

      [[displays]]
      connector = "${connector}"
      playlist = "p0"
    '') (mkOutputs count)
  );

  warmClient = pkgs.writeText "wall-in-one-rss-client.py" ''
    import json
    import os
    import socket

    endpoint = os.environ["WALL_IN_ONE_RSS_SOCKET"]
    output_count = int(os.environ["WALL_IN_ONE_RSS_OUTPUTS"])

    def call(verb: str, argument: str | None = None) -> str:
        request = {"verb": verb}
        if argument is not None:
            request["argument"] = argument
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # The listener exists before the initial 64-output apply finishes.
            # A slow CI runner may therefore queue this first connection for a
            # few seconds even though every later runtime command is tiny.
            client.settimeout(15)
            client.connect(endpoint)
            client.sendall(json.dumps(request).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                part = client.recv(1024 * 1024)
                if not part:
                    raise RuntimeError("runtime closed before replying")
                reply += part
        envelope = json.loads(reply)
        if not envelope.get("ok"):
            raise RuntimeError(f"{verb} failed: {envelope}")
        return envelope["message"]

    outputs = [f"OUT-{index}" for index in range(output_count)]
    initial = json.loads(call("status"))
    assert initial["status_version"] == 2, initial
    assert initial["display_mode"] == "independent", initial
    assert len(initial["displays"]) == output_count, len(initial["displays"])
    for connector in outputs:
        call("on", f"{connector} shuffle on")
        call("on", f"{connector} cycle off")
        call("on", f"{connector} next")
        call("on", f"{connector} previous")

    status = json.loads(call("status"))
    assert status["status_version"] == 2, status
    assert status["display_mode"] == "independent", status
    assert len(status["displays"]) == output_count, len(status["displays"])
    assert status["theme_source"]["effective"] == "OUT-0", status["theme_source"]
    assert all(display["connected"] for display in status["displays"]), status["displays"]
    print(json.dumps(status, sort_keys=True, separators=(",", ":")))
  '';
in
pkgs.runCommand "wall-in-one-service-rss"
  {
    nativeBuildInputs = [ pkgs.gawk pkgs.python314 pkgs.coreutils ];
  }
  ''
    mkdir -p "$out" "$TMPDIR/state" "$TMPDIR/runtime"
    service_pid=""
    cleanup() {
      if [ -n "$service_pid" ]; then
        kill -TERM "$service_pid" 2>/dev/null || true
        for attempt in $(${pkgs.coreutils}/bin/seq 1 100); do
          ! kill -0 "$service_pid" 2>/dev/null && break
          ${pkgs.coreutils}/bin/sleep 0.02
        done
        if kill -0 "$service_pid" 2>/dev/null; then
          echo "runtime ignored SIGTERM; forcing shutdown" >&2
          kill -KILL "$service_pid" 2>/dev/null || true
        fi
        wait "$service_pid" 2>/dev/null || true
        service_pid=""
      fi
    }
    trap cleanup EXIT

    printf 'shape\trun\tsample\trss_kib\n' > "$out/samples-kib.tsv"
    printf 'shape\trun\toutputs\telapsed_ms\tcpu_ticks\tcpu_millipercent\n' \
      > "$out/idle-cpu.tsv"
    clock_ticks=$(${pkgs.glibc.bin}/bin/getconf CLK_TCK)
    failed=0

    measure_shape() {
      label="$1"
      config_source="$2"
      output_count="$3"
      hard_limit="$4"
      config="$TMPDIR/state/runtime-$label.toml"
      cp "$config_source" "$config"
      shape_maximum=0
      shape_minimum=999999999
      shape_cpu_maximum=0
      shape_peaks=""

      for run in $(${pkgs.coreutils}/bin/seq 1 3); do
        socket="$TMPDIR/runtime/wall-in-one-runtime-$label-$run.sock"
        stdout="$TMPDIR/service-$label-$run.stdout"
        stderr="$TMPDIR/service-$label-$run.stderr"

        ${wallInOneService}/bin/wall-in-one-service \
          --config "$config" --socket "$socket" \
          >"$stdout" 2>"$stderr" &
        service_pid=$!

        for attempt in $(${pkgs.coreutils}/bin/seq 1 200); do
          [ -S "$socket" ] && break
          if ! kill -0 "$service_pid" 2>/dev/null; then
            cat "$stderr" >&2
            echo "runtime exited before its socket appeared in $label run $run" >&2
            exit 1
          fi
          ${pkgs.coreutils}/bin/sleep 0.05
        done
        if [ ! -S "$socket" ]; then
          cat "$stderr" >&2
          echo "runtime socket did not appear in $label run $run" >&2
          exit 1
        fi

        export WALL_IN_ONE_RSS_SOCKET="$socket"
        export WALL_IN_ONE_RSS_OUTPUTS="$output_count"
        if ! ${pkgs.python314}/bin/python ${warmClient} \
          > "$out/status-$label-run-$run.json"; then
          if kill -0 "$service_pid" 2>/dev/null; then
            echo "runtime remained alive after closing the client connection" >&2
          else
            wait "$service_pid" || exit_status=$?
            service_pid=""
            echo "runtime exited during socket warm-up with status ''${exit_status:-0}" >&2
          fi
          cat "$stdout" >&2
          cat "$stderr" >&2
          echo "runtime socket warm-up failed in $label run $run" >&2
          exit 1
        fi

        cpu_before=$(awk '{ print $14 + $15 }' "/proc/$service_pid/stat")
        time_before=$(${pkgs.coreutils}/bin/date +%s%N)
        ${pkgs.coreutils}/bin/sleep 6
        time_after=$(${pkgs.coreutils}/bin/date +%s%N)
        cpu_after=$(awk '{ print $14 + $15 }' "/proc/$service_pid/stat")
        elapsed_ms=$(( (time_after - time_before) / 1000000 ))
        cpu_ticks=$(( cpu_after - cpu_before ))
        cpu_millipercent=$(( cpu_ticks * 100000000 / (clock_ticks * elapsed_ms) ))
        [ "$cpu_millipercent" -gt "$shape_cpu_maximum" ] \
          && shape_cpu_maximum="$cpu_millipercent"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$label" "$run" "$output_count" "$elapsed_ms" "$cpu_ticks" \
          "$cpu_millipercent" >> "$out/idle-cpu.tsv"

        run_peak=0
        for sample in $(${pkgs.coreutils}/bin/seq 1 20); do
          if ! kill -0 "$service_pid" 2>/dev/null; then
            cat "$stderr" >&2
            echo "runtime exited during RSS sampling in $label run $run" >&2
            exit 1
          fi
          rss=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
          if [ -z "$rss" ]; then
            echo "could not read VmRSS for $service_pid in $label run $run" >&2
            exit 1
          fi
          [ "$rss" -gt "$shape_maximum" ] && shape_maximum="$rss"
          [ "$rss" -lt "$shape_minimum" ] && shape_minimum="$rss"
          [ "$rss" -gt "$run_peak" ] && run_peak="$rss"
          printf '%s\t%s\t%s\t%s\n' "$label" "$run" "$sample" "$rss" \
            >> "$out/samples-kib.tsv"
          ${pkgs.coreutils}/bin/sleep 0.05
        done
        shape_peaks="$shape_peaks $run_peak"
        cleanup
        cp "$stdout" "$out/service-$label-run-$run.stdout"
        cp "$stderr" "$out/service-$label-run-$run.stderr"
      done

      set -- $shape_peaks
      case "$label" in
        one)
          one_minimum="$shape_minimum"; one_maximum="$shape_maximum"
          one_peak_1="$1"; one_peak_2="$2"; one_peak_3="$3"
          one_cpu_maximum="$shape_cpu_maximum"
          ;;
        three)
          three_minimum="$shape_minimum"; three_maximum="$shape_maximum"
          three_peak_1="$1"; three_peak_2="$2"; three_peak_3="$3"
          three_cpu_maximum="$shape_cpu_maximum"
          ;;
      esac
      if [ "$shape_maximum" -gt "$hard_limit" ]; then
        echo "$label-display runtime exceeded its $hard_limit KiB RSS contract" >&2
        failed=1
      fi
    }

    measure_shape one ${mkRuntimeConfig 1} 1 5120
    measure_shape three ${mkRuntimeConfig 3} 3 10240

    # The supported connector ceiling is a routing/status correctness stress,
    # not a RAM promise for a real one- or three-monitor desktop.
    stress_config="$TMPDIR/state/runtime-stress.toml"
    stress_socket="$TMPDIR/runtime/wall-in-one-runtime-stress.sock"
    cp ${mkRuntimeConfig 64} "$stress_config"
    ${wallInOneService}/bin/wall-in-one-service \
      --config "$stress_config" --socket "$stress_socket" \
      >"$TMPDIR/service-stress.stdout" 2>"$TMPDIR/service-stress.stderr" &
    service_pid=$!
    for attempt in $(${pkgs.coreutils}/bin/seq 1 200); do
      [ -S "$stress_socket" ] && break
      if ! kill -0 "$service_pid" 2>/dev/null; then
        cat "$TMPDIR/service-stress.stderr" >&2
        echo "runtime exited before the 64-route stress socket appeared" >&2
        exit 1
      fi
      ${pkgs.coreutils}/bin/sleep 0.05
    done
    if [ ! -S "$stress_socket" ]; then
      cat "$TMPDIR/service-stress.stderr" >&2
      echo "64-route stress socket did not appear" >&2
      exit 1
    fi
    export WALL_IN_ONE_RSS_SOCKET="$stress_socket"
    export WALL_IN_ONE_RSS_OUTPUTS=64
    ${pkgs.python314}/bin/python ${warmClient} > "$out/status-stress-64.json"
    cleanup
    cp "$TMPDIR/service-stress.stdout" "$out/service-stress.stdout"
    cp "$TMPDIR/service-stress.stderr" "$out/service-stress.stderr"

    printf 'one_display_minimum_kib=%s\none_display_maximum_kib=%s\none_display_run_peaks_kib=%s,%s,%s\none_display_hard_limit_kib=5120\none_display_idle_cpu_max_millipercent=%s\nthree_display_minimum_kib=%s\nthree_display_maximum_kib=%s\nthree_display_run_peaks_kib=%s,%s,%s\nthree_display_hard_limit_kib=10240\nthree_display_idle_cpu_max_millipercent=%s\nstress_connectors=64\n' \
      "$one_minimum" "$one_maximum" "$one_peak_1" "$one_peak_2" "$one_peak_3" \
      "$one_cpu_maximum" "$three_minimum" "$three_maximum" "$three_peak_1" \
      "$three_peak_2" "$three_peak_3" "$three_cpu_maximum" > "$out/summary.txt"
    printf '{"one_display":{"minimum_kib":%s,"maximum_kib":%s,"run_peak_kib":[%s,%s,%s],"hard_limit_kib":5120,"idle_cpu_max_millipercent":%s},"three_display":{"minimum_kib":%s,"maximum_kib":%s,"run_peak_kib":[%s,%s,%s],"hard_limit_kib":10240,"idle_cpu_max_millipercent":%s},"stress_connectors":64}\n' \
      "$one_minimum" "$one_maximum" "$one_peak_1" "$one_peak_2" "$one_peak_3" \
      "$one_cpu_maximum" "$three_minimum" "$three_maximum" "$three_peak_1" \
      "$three_peak_2" "$three_peak_3" "$three_cpu_maximum" > "$out/summary.json"
    cat "$out/summary.txt"

    [ "$failed" -eq 0 ] || exit 1

    trap - EXIT
  ''
