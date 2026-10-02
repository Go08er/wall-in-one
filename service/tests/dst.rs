//! Status timing across America/Chicago's and Australia/Lord_Howe's 2026
//! daylight-saving changes, through chrono's real `Local` zone, and across a
//! change of zone while the process runs.
//!
//! `TZ` is process-wide, so this binary holds exactly one test and sets it
//! before anything reads the local zone. The values are POSIX rule strings,
//! not zone names, so they need no zone database in the build sandbox.

use chrono::{Local, NaiveDate, NaiveDateTime, TimeZone, Utc};
use std::path::PathBuf;
use std::time::Duration;
use wall_in_one_service::config::{Config, ScheduleRule};
use wall_in_one_service::protocol::Request;
use wall_in_one_service::renderer::WallpaperDriver;
use wall_in_one_service::runtime::{Runtime, wall_clock_after};

/// US Central time: CST (UTC-6), and CDT (UTC-5) from the second Sunday of
/// March to the first Sunday of November.
const CHICAGO: &str = "CST6CDT,M3.2.0,M11.1.0";

/// Lord Howe Island, as its zone file states it: UTC+10:30, and UTC+11 from
/// the first Sunday of October to the first Sunday of April; the clock moves
/// half an hour at 02:00.
const LORD_HOWE: &str = "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0";

struct Quiet;

impl WallpaperDriver for Quiet {
    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        Ok(Vec::new())
    }
    fn apply(
        &mut self,
        _entry: &wall_in_one_service::config::Entry,
        _output: &str,
        _settings: &wall_in_one_service::config::Settings,
    ) -> Result<(), String> {
        Ok(())
    }
    fn set_paused(&mut self, _paused: bool) -> Result<(), String> {
        Ok(())
    }
    fn reconfigure(&mut self, _settings: wall_in_one_service::config::RendererSettings) {}
    fn stop(&mut self) {}
}

fn local(month: u32, day: u32, hour: u32, minute: u32) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, month, day)
        .unwrap()
        .and_hms_opt(hour, minute, 0)
        .unwrap()
}

/// The single display row of a mirrored runtime with `rule`, judged at `at`.
fn row(rule: ScheduleRule, at: NaiveDateTime) -> serde_json::Value {
    let document = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/minimal-runtime.toml"
    ))
    .unwrap();
    let mut config: Config = toml::from_str(&document).unwrap();
    config.schedules = vec![rule];
    config.validate().unwrap();
    let mut runtime = Runtime::new(PathBuf::from("/tmp/runtime.toml"), config, Quiet, at).unwrap();
    runtime.apply_current().unwrap();
    let response = runtime.handle(
        Request {
            verb: "status".into(),
            argument: None,
        },
        at,
    );
    assert!(response.ok, "{}", response.message);
    let status: serde_json::Value = serde_json::from_str(&response.message).unwrap();
    status["displays"][0].clone()
}

fn window(start: &str, end: &str) -> ScheduleRule {
    ScheduleRule {
        id: "window".into(),
        playlist: "one".into(),
        connector: String::new(),
        months: vec![],
        weekdays: vec![],
        start: Some(start.into()),
        end: Some(end.into()),
        enabled: true,
    }
}

#[test]
fn timing_follows_the_local_clock_across_daylight_saving_and_zone_changes() {
    // SAFETY: this binary's only test, run before anything reads `TZ`.
    unsafe { std::env::set_var("TZ", CHICAGO) };
    assert_eq!(
        Local
            .offset_from_utc_datetime(&local(8, 3, 12, 0))
            .local_minus_utc(),
        -5 * 3600,
        "chrono must read the POSIX rule from TZ"
    );

    // The cycle deadline is elapsed time projected through UTC: ten minutes
    // after 01:55 CST on 2026-03-08 the clock reads 03:05 CDT, and ten minutes
    // after 01:55 CDT on 2026-11-01 it reads 01:05 CST.
    let spring = Utc.from_utc_datetime(&local(3, 8, 7, 55));
    assert_eq!(
        wall_clock_after(spring, 600, &Local),
        Some(local(3, 8, 3, 5))
    );
    let fall = Utc.from_utc_datetime(&local(11, 1, 6, 55));
    assert_eq!(
        wall_clock_after(fall, 600, &Local),
        Some(local(11, 1, 1, 5))
    );

    // A schedule boundary inside the spring gap acts, and is reported, when
    // the clock reaches the gap's end, an hour of real time after 01:00 CST.
    let gap = row(window("02:30", "06:00"), local(3, 8, 1, 0));
    assert_eq!(gap["route_source"], "default");
    assert_eq!(gap["route_change_at"], "2026-03-08T03:00:00");
    assert_eq!(gap["until"], "03:00");
    assert_eq!(gap["route_change_in_s"], 3600);
    // A window that lies entirely inside the gap changes nothing that day.
    let lost = row(window("02:15", "02:45"), local(3, 8, 1, 0));
    assert_eq!(lost["route_change_at"], "2026-03-09T02:15:00");

    // A reading the caller supplies is its first instant, so 01:55 on
    // 2026-11-01 is daylight time, five minutes before the clock goes back.
    // The audit's case: a 01:30-02:30 window matches now and no longer does
    // once the clock reads 01:00 again, so the route changes then, not at
    // 02:30.
    let rewound = row(window("01:30", "02:30"), local(11, 1, 1, 55));
    assert_eq!(rewound["route_source"], "schedule");
    assert_eq!(rewound["route_change_at"], "2026-11-01T01:00:00");
    assert_eq!(rewound["until"], "01:00");
    assert_eq!(rewound["route_change_in_s"], 300);
    // A window that still matches at 01:00 is no change there: it ends at
    // 02:30 standard time, 95 real minutes on.
    let kept = row(window("00:30", "02:30"), local(11, 1, 1, 55));
    assert_eq!(kept["route_source"], "schedule");
    assert_eq!(kept["route_change_at"], "2026-11-01T02:30:00");
    assert_eq!(kept["until"], "02:30");
    assert_eq!(kept["route_change_in_s"], 95 * 60);
    // Before the repeated hour, its first 01:30 comes first.
    let early = row(window("01:30", "06:00"), local(11, 1, 0, 30));
    assert_eq!(early["until"], "01:30");
    assert_eq!(early["route_change_in_s"], 3600);

    // The zone can change under a running service. Status builds its timeline
    // afresh for every reply from chrono's `Local`, which re-reads `TZ` (or,
    // without it, /etc/localtime's own modification time) at most once a
    // second, so a reply a second after the change uses the new zone.
    // SAFETY: still this binary's only test.
    unsafe { std::env::set_var("TZ", LORD_HOWE) };
    std::thread::sleep(Duration::from_millis(1100));
    assert_eq!(
        Local
            .offset_from_utc_datetime(&local(8, 3, 0, 0))
            .local_minus_utc(),
        10 * 3600 + 1800,
        "the next reading must use the new zone"
    );

    // Lord Howe goes back half an hour at 02:00 on 2026-04-05: at the first
    // 01:50 a 01:45-02:30 window matches, and ten minutes later the clock
    // reads 01:30, where it does not.
    let rewound = row(window("01:45", "02:30"), local(4, 5, 1, 50));
    assert_eq!(rewound["route_source"], "schedule");
    assert_eq!(rewound["route_change_at"], "2026-04-05T01:30:00");
    assert_eq!(rewound["until"], "01:30");
    assert_eq!(rewound["route_change_in_s"], 600);
    // A window still matching at 01:30 ends at 03:00, 100 real minutes on.
    let kept = row(window("01:00", "03:00"), local(4, 5, 1, 50));
    assert_eq!(kept["route_change_at"], "2026-04-05T03:00:00");
    assert_eq!(kept["route_change_in_s"], 100 * 60);
    // It goes forward half an hour at 02:00 on 2026-10-04: a window from
    // 02:15 is first seen at 02:30, ten minutes after 01:50.
    let gap = row(window("02:15", "05:00"), local(10, 4, 1, 50));
    assert_eq!(gap["route_change_at"], "2026-10-04T02:30:00");
    assert_eq!(gap["until"], "02:30");
    assert_eq!(gap["route_change_in_s"], 600);
    // And the rotation's wall-clock time follows the same clock: ten minutes
    // after the first 01:55 on 2026-04-05 it reads 01:35.
    let first_pass = Utc.from_utc_datetime(
        &NaiveDate::from_ymd_opt(2026, 4, 4)
            .unwrap()
            .and_hms_opt(14, 55, 0)
            .unwrap(),
    );
    assert_eq!(
        wall_clock_after(first_pass, 600, &Local),
        Some(local(4, 5, 1, 35))
    );
}
