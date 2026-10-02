//! Status timing across America/Chicago's 2026 daylight-saving changes,
//! through chrono's real `Local` zone.
//!
//! `TZ` is process-wide, so this binary holds exactly one test and sets it
//! before anything reads the local zone. The value is a POSIX rule string,
//! not a zone name, so it needs no zone database in the build sandbox.

use chrono::{Local, NaiveDate, NaiveDateTime, TimeZone, Utc};
use std::path::PathBuf;
use wall_in_one_service::config::{Config, ScheduleRule};
use wall_in_one_service::protocol::Request;
use wall_in_one_service::renderer::WallpaperDriver;
use wall_in_one_service::runtime::{Runtime, wall_clock_after};

/// US Central time: CST (UTC-6), and CDT (UTC-5) from the second Sunday of
/// March to the first Sunday of November.
const CHICAGO: &str = "CST6CDT,M3.2.0,M11.1.0";

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
fn timing_follows_the_local_clock_across_both_chicago_changes() {
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
    // the clock reaches the gap's end.
    let gap = row(window("02:30", "06:00"), local(3, 8, 1, 0));
    assert_eq!(gap["route_source"], "default");
    assert_eq!(gap["route_change_at"], "2026-03-08T03:00:00");
    assert_eq!(gap["until"], "03:00");
    // A window that lies entirely inside the gap changes nothing that day.
    let lost = row(window("02:15", "02:45"), local(3, 8, 1, 0));
    assert_eq!(lost["route_change_at"], "2026-03-09T02:15:00");
    // On the autumn change the repeated hour exists, so a boundary in it is
    // reported as it reads; its first occurrence is the one that comes next.
    let repeated = row(window("01:30", "06:00"), local(11, 1, 0, 30));
    assert_eq!(repeated["until"], "01:30");
}
