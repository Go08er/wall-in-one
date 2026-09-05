{ pkgs, wallInOneService }:

let
  lib = pkgs.lib;
  playlists = builtins.genList (index: index) 4;
  # Model a library at the scale where the GTK grid's old rebuild strategy
  # first became visibly expensive.  The generated All media playlist carries
  # every item; three ordinary authored playlists each reference a useful
  # subset.  That is 600 library items and 900 resolved runtime occurrences,
  # rather than a tiny fixture which flatters the daemon's heap usage.
  mkOutputs = count: builtins.genList (index: "OUT-${toString index}") count;

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
  privateBusConfig = pkgs.writeText "wall-in-one-rss-private-bus.conf" ''
    <busconfig>
      <type>session</type>
      <listen>unix:tmpdir=/tmp</listen>
      <auth>EXTERNAL</auth>
      <policy context="default">
        <allow send_destination="*"/>
        <allow receive_sender="*"/>
        <allow own="*"/>
      </policy>
    </busconfig>
  '';

  mkRuntimeConfigWithBattery = batteryEnabled: outputCount: libraryCount: authoredCount:
    let
      libraryEntries = builtins.genList (index: index) libraryCount;
      authoredEntries = builtins.genList (index: index) authoredCount;
      entriesFor = playlist: if playlist == 0 then libraryEntries else authoredEntries;
    in
    pkgs.writeText
      "wall-in-one-rss-${toString outputCount}-${toString libraryCount}-runtime.toml"
      (''
      schema_version = ${if batteryEnabled then "5" else "4"}
      config_generation = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      default_playlist = "p0"

      [settings]
      cycle_interval_seconds = 300
      cycle_enabled = false
      shuffle = false
      dynamics_enabled = false
      ${lib.optionalString batteryEnabled "stop_animations_on_battery = true"}
      display_mode = "independent"
      theme_source_connector = "OUT-0"

      [renderer]
      noctalia_program = "${fakeNoctalia}"
      niri_program = "${mkFakeNiri outputCount}"
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
      name = "${if playlist == 0 then "All media" else "Playlist ${toString playlist}"}"
      ${lib.concatMapStrings (entry: ''

      [[playlists.entries]]
      id = "p${toString playlist}-e${toString entry}"
      kind = "still"
      still = "/wallpapers/library-${toString entry}.jpg"
      palette = { kind = "keep", mode = "keep" }
      '') (entriesFor playlist)}
    '') playlists
    + lib.concatMapStrings (connector: ''

      [[displays]]
      connector = "${connector}"
      playlist = "p0"
    '') (mkOutputs outputCount)
  );

  mkRuntimeConfig = mkRuntimeConfigWithBattery false;

  warmClient = pkgs.writeText "wall-in-one-rss-client.py" ''
    import json
    import os
    import socket

    endpoint = os.environ["WALL_IN_ONE_RSS_SOCKET"]
    output_count = int(os.environ["WALL_IN_ONE_RSS_OUTPUTS"])
    library_count = int(os.environ["WALL_IN_ONE_RSS_LIBRARY"])
    authored_count = int(os.environ["WALL_IN_ONE_RSS_AUTHORED"])

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
    def check_power(status: dict) -> None:
        enabled = os.environ.get("WALL_IN_ONE_RSS_BATTERY") == "1"
        source = os.environ.get("WALL_IN_ONE_RSS_POWER_SOURCE", "unknown")
        assert status["stop_animations_on_battery"] is enabled, status
        assert status["power_source"] == source, status
        assert status["power_available"] is (source != "unknown"), status
        assert status["animations_inhibited"] is (source == "battery"), status
        assert status["animation_inhibition_reason"] == (
            "battery" if source == "battery" else ""
        ), status

    initial = json.loads(call("status"))
    check_power(initial)
    assert initial["status_version"] == 2, initial
    assert initial["display_mode"] == "independent", initial
    assert len(initial["displays"]) == output_count, len(initial["displays"])
    inventory = {playlist["id"]: playlist["entries"] for playlist in initial["playlists"]}
    assert inventory == {
        "p0": library_count,
        "p1": authored_count,
        "p2": authored_count,
        "p3": authored_count,
    }, inventory
    if os.environ.get("WALL_IN_ONE_RSS_STATUS_ONLY") == "1":
        print(json.dumps(initial, sort_keys=True, separators=(",", ":")))
        raise SystemExit(0)
    for connector in outputs:
        call("on", f"{connector} shuffle on")
        call("on", f"{connector} cycle off")
        call("on", f"{connector} next")
        call("on", f"{connector} previous")

    status = json.loads(call("status"))
    check_power(status)
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
    # Even feature-enabled measurements must never inspect the builder's real
    # system bus. Exercise quiet bounded retries against a missing private path.
    export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$TMPDIR/no-power-bus"
    service_pid=""
    power_provider_pid=""
    power_bus_pid=""
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
    cleanup_power_fixture() {
      for fixture_pid in "$power_provider_pid" "$power_bus_pid"; do
        [ -n "$fixture_pid" ] || continue
        kill -TERM "$fixture_pid" 2>/dev/null || true
        for attempt in $(${pkgs.coreutils}/bin/seq 1 100); do
          ! kill -0 "$fixture_pid" 2>/dev/null && break
          ${pkgs.coreutils}/bin/sleep 0.02
        done
        if kill -0 "$fixture_pid" 2>/dev/null; then
          kill -KILL "$fixture_pid" 2>/dev/null || true
        fi
        wait "$fixture_pid" 2>/dev/null || true
      done
      power_provider_pid=""
      power_bus_pid=""
      export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$TMPDIR/no-power-bus"
      unset WALL_IN_ONE_RSS_POWER_SOURCE
    }
    trap 'cleanup; cleanup_power_fixture' EXIT

    start_power_fixture() {
      power_source="$1"
      bus_address_file="$TMPDIR/power-$power_source.address"
      provider_ready_file="$TMPDIR/power-$power_source.ready"
      # No host configuration or service-activation directories are loaded.
      ${pkgs.dbus}/bin/dbus-daemon --config-file ${privateBusConfig} \
        --nofork --nopidfile --print-address=1 \
        > "$bus_address_file" 2> "$out/power-$power_source-bus.stderr" &
      power_bus_pid=$!
      for attempt in $(${pkgs.coreutils}/bin/seq 1 200); do
        [ -s "$bus_address_file" ] && break
        if ! kill -0 "$power_bus_pid" 2>/dev/null; then
          cat "$out/power-$power_source-bus.stderr" >&2
          echo "disposable power bus exited before readiness" >&2
          exit 1
        fi
        ${pkgs.coreutils}/bin/sleep 0.05
      done
      DBUS_SYSTEM_BUS_ADDRESS=$(${pkgs.coreutils}/bin/head -n 1 "$bus_address_file")
      case "$DBUS_SYSTEM_BUS_ADDRESS" in
        unix:*) ;;
        *) echo "disposable power bus did not publish a private address" >&2; exit 1 ;;
      esac
      export DBUS_SYSTEM_BUS_ADDRESS
      ${pkgs.python314}/bin/python ${./fake-upower.py} \
        ${lib.getLib pkgs.dbus}/lib/libdbus-1.so.3 \
        "$DBUS_SYSTEM_BUS_ADDRESS" "$power_source" \
        > "$provider_ready_file" 2> "$out/power-$power_source-provider.stderr" &
      power_provider_pid=$!
      for attempt in $(${pkgs.coreutils}/bin/seq 1 200); do
        [ -s "$provider_ready_file" ] && break
        if ! kill -0 "$power_provider_pid" 2>/dev/null; then
          cat "$out/power-$power_source-provider.stderr" >&2
          echo "disposable UPower provider exited before readiness" >&2
          exit 1
        fi
        ${pkgs.coreutils}/bin/sleep 0.05
      done
      if [ "$(${pkgs.coreutils}/bin/head -n 1 "$provider_ready_file")" != READY ]; then
        echo "disposable UPower provider did not become ready" >&2
        exit 1
      fi
      export WALL_IN_ONE_RSS_POWER_SOURCE="$power_source"
    }

    printf 'shape\trun\tsample\trss_kib\n' > "$out/samples-kib.tsv"
    printf 'shape\trun\toutputs\telapsed_ms\tclock_ticks_per_second\tcpu_ticks\tcpu_millipercent\n' \
      > "$out/idle-cpu.tsv"
    printf 'library_items\tresolved_entry_occurrences\trss_kib\tstatus_bytes\n' \
      > "$out/library-slope.tsv"
    printf 'shape\trun\trss_before_request_kib\trss_after_status_kib\trss_after_controls_kib\n' \
      > "$out/warmup-rss.tsv"
    printf 'shape\trun\tthreads\tfile_descriptors\n' > "$out/process-counts.tsv"
    clock_ticks=$(${pkgs.glibc.bin}/bin/getconf CLK_TCK)
    if [ -z "$clock_ticks" ] || [ "$clock_ticks" -le 0 ]; then
      echo "could not read a positive CLK_TCK" >&2
      exit 1
    fi
    service_binary_bytes=$(wc -c < ${wallInOneService}/bin/wall-in-one-service)
    failed=0

    capture_run_peak() {
      observed_rss="$1"
      if [ -z "$observed_rss" ]; then
        echo "could not read peak VmRSS in $label run $run" >&2
        exit 1
      fi
      if [ "$observed_rss" -gt "$run_peak" ]; then
        run_peak="$observed_rss"
        printf '%s\n' "$observed_rss" > "$out/peak-rss-$label-run-$run-kib.txt"
        # procfs sources are read-only, so their copied artifacts are too.
        # Replace only these gate-owned snapshots when a later peak wins.
        cp --remove-destination "/proc/$service_pid/status" "$out/peak-proc-status-$label-run-$run.txt"
        cp --remove-destination "/proc/$service_pid/maps" "$out/peak-maps-$label-run-$run.txt"
        if [ -r "/proc/$service_pid/smaps" ]; then
          cp --remove-destination "/proc/$service_pid/smaps" "$out/peak-smaps-$label-run-$run.txt"
        fi
      fi
    }

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
        run_peak=0

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
        export WALL_IN_ONE_RSS_LIBRARY=600
        export WALL_IN_ONE_RSS_AUTHORED=100
        # The listener is bound just before initial_apply. Its fake helpers are
        # immediate, so one scheduler grace leaves a useful pre-request model
        # observation without adding another measurement run.
        ${pkgs.coreutils}/bin/sleep 0.1
        rss_before_request=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
        capture_run_peak "$rss_before_request"
        export WALL_IN_ONE_RSS_STATUS_ONLY=1
        if ! ${pkgs.python314}/bin/python ${warmClient} \
          > "$out/status-$label-run-$run-before-controls.json"; then
          cat "$stdout" >&2
          cat "$stderr" >&2
          echo "runtime status warm-up failed in $label run $run" >&2
          exit 1
        fi
        rss_after_status=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
        capture_run_peak "$rss_after_status"
        unset WALL_IN_ONE_RSS_STATUS_ONLY
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
        rss_after_controls=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
        capture_run_peak "$rss_after_controls"
        printf '%s\t%s\t%s\t%s\t%s\n' \
          "$label" "$run" "$rss_before_request" "$rss_after_status" \
          "$rss_after_controls" \
          >> "$out/warmup-rss.tsv"
        for rss in "$rss_before_request" "$rss_after_status" "$rss_after_controls"; do
          if [ -z "$rss" ]; then
            echo "could not read warm-up VmRSS in $label run $run" >&2
            exit 1
          fi
          [ "$rss" -gt "$shape_maximum" ] && shape_maximum="$rss"
          [ "$rss" -lt "$shape_minimum" ] && shape_minimum="$rss"
          [ "$rss" -gt "$run_peak" ] && run_peak="$rss"
        done
        cp "/proc/$service_pid/status" "$out/proc-status-$label-run-$run.txt"
        if [ -r "/proc/$service_pid/smaps_rollup" ]; then
          cp "/proc/$service_pid/smaps_rollup" "$out/smaps-$label-run-$run.txt"
        fi

        cpu_before=$(awk '{ print $14 + $15 }' "/proc/$service_pid/stat")
        time_before=$(${pkgs.coreutils}/bin/date +%s%N)
        # Ten seconds spans two five-second compositor discovery intervals and
        # reduces the quantisation error of Linux's scheduler-tick accounting.
        ${pkgs.coreutils}/bin/sleep 10
        time_after=$(${pkgs.coreutils}/bin/date +%s%N)
        cpu_after=$(awk '{ print $14 + $15 }' "/proc/$service_pid/stat")
        expected_threads=1
        [ "''${WALL_IN_ONE_RSS_BATTERY:-0}" = 1 ] && expected_threads=2
        # The ten-second boundary can coincide with a five-second compositor
        # probe. Allow its owned capture threads to finish before judging
        # resident worker/FD counts; an accumulating leak never settles.
        for attempt in $(${pkgs.coreutils}/bin/seq 1 20); do
          threads=$(awk '$1 == "Threads:" { print $2 }' "/proc/$service_pid/status")
          service_fds=( "/proc/$service_pid/fd/"* )
          descriptors=''${#service_fds[@]}
          if [ "$threads" -eq "$expected_threads" ] && [ "$descriptors" -le 9 ]; then
            break
          fi
          ${pkgs.coreutils}/bin/sleep 0.05
        done
        printf '%s\t%s\t%s\t%s\n' "$label" "$run" "$threads" "$descriptors" \
          >> "$out/process-counts.tsv"
        if [ "$threads" -ne "$expected_threads" ]; then
          echo "$label run $run retained $threads threads; expected $expected_threads" >&2
          failed=1
        fi
        # stdio, listener and singleton lock; connected power adds one socket.
        # Leave room for one short-lived helper's captured output descriptors,
        # while refusing an accumulating connection/pipe leak.
        if [ "$descriptors" -lt 5 ] || [ "$descriptors" -gt 9 ]; then
          echo "$label run $run retained an unexpected $descriptors file descriptors" >&2
          failed=1
        fi
        if [ -n "$power_provider_pid" ]; then
          if ! kill -0 "$power_provider_pid" || ! kill -0 "$power_bus_pid"; then
            echo "$label run $run lost its disposable power fixture" >&2
            exit 1
          fi
          export WALL_IN_ONE_RSS_STATUS_ONLY=1
          ${pkgs.python314}/bin/python ${warmClient} \
            > "$out/status-$label-run-$run-after-idle.json"
          unset WALL_IN_ONE_RSS_STATUS_ONLY
        fi
        elapsed_ms=$(( (time_after - time_before) / 1000000 ))
        cpu_ticks=$(( cpu_after - cpu_before ))
        if [ "$elapsed_ms" -le 0 ] || [ "$cpu_ticks" -lt 0 ]; then
          echo "invalid idle CPU sample in $label run $run" >&2
          exit 1
        fi
        # millipercent = 1000 * percent. elapsed_ms is deliberately rounded
        # down, so this slightly overstates rather than hides CPU usage. Use
        # cross multiplication for the hard gate: truncating the displayed
        # quotient must never let a value just over 2% pass.
        cpu_numerator=$(( cpu_ticks * 100000000 ))
        cpu_denominator=$(( clock_ticks * elapsed_ms ))
        cpu_millipercent=$(( cpu_numerator / cpu_denominator ))
        if [ "$cpu_numerator" -gt $(( 2000 * cpu_denominator )) ]; then
          echo "$label-display runtime exceeded 2% of one CPU while idle in run $run" >&2
          failed=1
        fi
        [ "$cpu_millipercent" -gt "$shape_cpu_maximum" ] \
          && shape_cpu_maximum="$cpu_millipercent"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$label" "$run" "$output_count" "$elapsed_ms" "$clock_ticks" \
          "$cpu_ticks" "$cpu_millipercent" >> "$out/idle-cpu.tsv"

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
          capture_run_peak "$rss"
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
        one-battery)
          battery_one_maximum="$shape_maximum"
          battery_one_cpu_maximum="$shape_cpu_maximum"
          ;;
        three-battery)
          battery_three_maximum="$shape_maximum"
          battery_three_cpu_maximum="$shape_cpu_maximum"
          ;;
      esac
      if [ "$shape_maximum" -gt "$hard_limit" ]; then
        echo "$label-display runtime exceeded its $hard_limit KiB RSS contract: min=$shape_minimum KiB, max=$shape_maximum KiB, run peaks=$shape_peaks KiB" >&2
        failed=1
      fi
    }

    measure_shape one ${mkRuntimeConfig 1 600 100} 1 5120
    measure_shape three ${mkRuntimeConfig 3 600 100} 3 10240

    export WALL_IN_ONE_RSS_BATTERY=1
    measure_shape one-battery ${mkRuntimeConfigWithBattery true 1 600 100} 1 5120
    measure_shape three-battery ${mkRuntimeConfigWithBattery true 3 600 100} 3 10240
    unset WALL_IN_ONE_RSS_BATTERY
    printf '{"power_source":"unknown","bus":"missing-private","one_display":{"maximum_kib":%s,"hard_limit_kib":5120,"idle_cpu_max_millipercent":%s},"three_display":{"maximum_kib":%s,"hard_limit_kib":10240,"idle_cpu_max_millipercent":%s}}\n' \
      "$battery_one_maximum" "$battery_one_cpu_maximum" \
      "$battery_three_maximum" "$battery_three_cpu_maximum" > "$out/battery-summary.json"

    # Measure the actual authenticated/subscribed worker, including its bus
    # buffers, on both authoritative power states. The fixture's own process
    # memory/CPU is intentionally outside the service-process contract.
    export WALL_IN_ONE_RSS_BATTERY=1
    for power_source in ac battery; do
      start_power_fixture "$power_source"
      measure_shape "one-power-$power_source" ${mkRuntimeConfigWithBattery true 1 600 100} 1 5120
      printf '{"power_source":"%s","outputs":1,"maximum_kib":%s,"hard_limit_kib":5120,"idle_cpu_max_millipercent":%s}\n' \
        "$power_source" "$shape_maximum" "$shape_cpu_maximum" > "$out/power-$power_source-one-summary.json"
      measure_shape "three-power-$power_source" ${mkRuntimeConfigWithBattery true 3 600 100} 3 10240
      printf '{"power_source":"%s","outputs":3,"maximum_kib":%s,"hard_limit_kib":10240,"idle_cpu_max_millipercent":%s}\n' \
        "$power_source" "$shape_maximum" "$shape_cpu_maximum" > "$out/power-$power_source-three-summary.json"
      cleanup_power_fixture
    done
    unset WALL_IN_ONE_RSS_BATTERY

    # One fresh sample at smaller library sizes makes allocator regressions
    # diagnosable without weakening the representative 600-item contract.
    # These are observations, not alternative gates.
    measure_library_scale() {
      library_count="$1"
      authored_count="$2"
      config_source="$3"
      resolved_count=$(( library_count + 3 * authored_count ))
      label="scale-$library_count"
      config="$TMPDIR/state/runtime-$label.toml"
      socket="$TMPDIR/runtime/wall-in-one-runtime-$label.sock"
      stdout="$TMPDIR/service-$label.stdout"
      stderr="$TMPDIR/service-$label.stderr"
      status="$out/status-$label.json"
      cp "$config_source" "$config"

      ${wallInOneService}/bin/wall-in-one-service \
        --config "$config" --socket "$socket" \
        >"$stdout" 2>"$stderr" &
      service_pid=$!
      for attempt in $(${pkgs.coreutils}/bin/seq 1 200); do
        [ -S "$socket" ] && break
        if ! kill -0 "$service_pid" 2>/dev/null; then
          cat "$stderr" >&2
          echo "runtime exited before its socket appeared for $library_count items" >&2
          exit 1
        fi
        ${pkgs.coreutils}/bin/sleep 0.05
      done
      if [ ! -S "$socket" ]; then
        cat "$stderr" >&2
        echo "runtime socket did not appear for $library_count items" >&2
        exit 1
      fi

      export WALL_IN_ONE_RSS_SOCKET="$socket"
      export WALL_IN_ONE_RSS_OUTPUTS=1
      export WALL_IN_ONE_RSS_LIBRARY="$library_count"
      export WALL_IN_ONE_RSS_AUTHORED="$authored_count"
      ${pkgs.coreutils}/bin/sleep 0.1
      rss_before_request=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
      export WALL_IN_ONE_RSS_STATUS_ONLY=1
      ${pkgs.python314}/bin/python ${warmClient} > "$out/status-$label-before-controls.json"
      rss_after_status=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
      unset WALL_IN_ONE_RSS_STATUS_ONLY
      ${pkgs.python314}/bin/python ${warmClient} > "$status"
      rss_after_controls=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
      printf '%s\t1\t%s\t%s\t%s\n' \
        "$label" "$rss_before_request" "$rss_after_status" "$rss_after_controls" \
        >> "$out/warmup-rss.tsv"
      cp "/proc/$service_pid/status" "$out/proc-status-$label.txt"
      if [ -r "/proc/$service_pid/smaps_rollup" ]; then
        cp "/proc/$service_pid/smaps_rollup" "$out/smaps-$label.txt"
      fi
      scale_peak=0
      for rss in "$rss_before_request" "$rss_after_status" "$rss_after_controls"; do
        if [ -z "$rss" ]; then
          echo "could not read warm-up VmRSS at $library_count items" >&2
          exit 1
        fi
        [ "$rss" -gt "$scale_peak" ] && scale_peak="$rss"
      done
      for sample in $(${pkgs.coreutils}/bin/seq 1 20); do
        rss=$(awk '$1 == "VmRSS:" { print $2 }' "/proc/$service_pid/status")
        if [ -z "$rss" ]; then
          echo "could not read VmRSS for $service_pid at $library_count items" >&2
          exit 1
        fi
        [ "$rss" -gt "$scale_peak" ] && scale_peak="$rss"
        ${pkgs.coreutils}/bin/sleep 0.05
      done
      status_bytes=$(wc -c < "$status")
      printf '%s\t%s\t%s\t%s\n' \
        "$library_count" "$resolved_count" "$scale_peak" "$status_bytes" \
        >> "$out/library-slope.tsv"
      cleanup
      cp "$stdout" "$out/service-$label.stdout"
      cp "$stderr" "$out/service-$label.stderr"
    }

    measure_library_scale 64 10 ${mkRuntimeConfig 1 64 10}
    measure_library_scale 300 50 ${mkRuntimeConfig 1 300 50}
    one_status_bytes=$(wc -c < "$out/status-one-run-1.json")
    printf '600\t900\t%s\t%s\n' "$one_maximum" "$one_status_bytes" \
      >> "$out/library-slope.tsv"

    # The supported connector ceiling is a routing/status correctness stress,
    # not a RAM promise for a real one- or three-monitor desktop.
    stress_config="$TMPDIR/state/runtime-stress.toml"
    stress_socket="$TMPDIR/runtime/wall-in-one-runtime-stress.sock"
    cp ${mkRuntimeConfig 64 600 100} "$stress_config"
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
    export WALL_IN_ONE_RSS_LIBRARY=600
    export WALL_IN_ONE_RSS_AUTHORED=100
    ${pkgs.python314}/bin/python ${warmClient} > "$out/status-stress-64.json"
    cleanup
    cp "$TMPDIR/service-stress.stdout" "$out/service-stress.stdout"
    cp "$TMPDIR/service-stress.stderr" "$out/service-stress.stderr"

    printf 'service_binary_bytes=%s\nlibrary_items=600\nresolved_entry_occurrences=900\none_display_minimum_kib=%s\none_display_maximum_kib=%s\none_display_run_peaks_kib=%s,%s,%s\none_display_hard_limit_kib=5120\none_display_idle_cpu_max_millipercent=%s\nthree_display_minimum_kib=%s\nthree_display_maximum_kib=%s\nthree_display_run_peaks_kib=%s,%s,%s\nthree_display_hard_limit_kib=10240\nthree_display_idle_cpu_max_millipercent=%s\nclock_ticks_per_second=%s\nidle_cpu_hard_limit_millipercent=2000\nidle_cpu_formula=cpu_ticks*100000000/(clock_ticks_per_second*elapsed_ms)\nstress_connectors=64\n' \
      "$service_binary_bytes" \
      "$one_minimum" "$one_maximum" "$one_peak_1" "$one_peak_2" "$one_peak_3" \
      "$one_cpu_maximum" "$three_minimum" "$three_maximum" "$three_peak_1" \
      "$three_peak_2" "$three_peak_3" "$three_cpu_maximum" "$clock_ticks" \
      > "$out/summary.txt"
    printf '{"service_binary_bytes":%s,"library_items":600,"resolved_entry_occurrences":900,"one_display":{"minimum_kib":%s,"maximum_kib":%s,"run_peak_kib":[%s,%s,%s],"hard_limit_kib":5120,"idle_cpu_max_millipercent":%s},"three_display":{"minimum_kib":%s,"maximum_kib":%s,"run_peak_kib":[%s,%s,%s],"hard_limit_kib":10240,"idle_cpu_max_millipercent":%s},"clock_ticks_per_second":%s,"idle_cpu_hard_limit_millipercent":2000,"stress_connectors":64}\n' \
      "$service_binary_bytes" \
      "$one_minimum" "$one_maximum" "$one_peak_1" "$one_peak_2" "$one_peak_3" \
      "$one_cpu_maximum" "$three_minimum" "$three_maximum" "$three_peak_1" \
      "$three_peak_2" "$three_peak_3" "$three_cpu_maximum" "$clock_ticks" \
      > "$out/summary.json"
    cat "$out/summary.txt"

    [ "$failed" -eq 0 ] || exit 1

    trap - EXIT
  ''
