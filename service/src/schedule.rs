use crate::config::{ConfigError, ScheduleRule, parse_time};
use chrono::{
    DateTime, Datelike, Days, LocalResult, NaiveDateTime, Offset, TimeDelta, TimeZone, Timelike,
    Utc,
};

pub trait Clock {
    fn now(&self) -> NaiveDateTime;
}
pub struct LocalClock;
impl Clock for LocalClock {
    fn now(&self) -> NaiveDateTime {
        chrono::Local::now().naive_local()
    }
}

pub fn resolve<'a>(
    rules: &'a [ScheduleRule],
    fallback: &'a str,
    at: NaiveDateTime,
) -> Result<&'a str, ConfigError> {
    Ok(resolve_override(rules, at)?.unwrap_or(fallback))
}

pub fn resolve_override(
    rules: &[ScheduleRule],
    at: NaiveDateTime,
) -> Result<Option<&str>, ConfigError> {
    Ok(resolve_rule(rules, at)?.map(|rule| rule.playlist.as_str()))
}

pub fn resolve_rule(
    rules: &[ScheduleRule],
    at: NaiveDateTime,
) -> Result<Option<&ScheduleRule>, ConfigError> {
    resolve_rule_for(rules, None, at)
}

pub fn resolve_override_for<'a>(
    rules: &'a [ScheduleRule],
    connector: &str,
    at: NaiveDateTime,
) -> Result<Option<&'a str>, ConfigError> {
    Ok(resolve_rule_for(rules, Some(connector), at)?.map(|rule| rule.playlist.as_str()))
}

/// Resolve the last matching rule visible to one routing scope.
///
/// The mirrored/global scope sees only rules without a connector. An
/// independent connector sees both global and connector-specific rules in
/// authored order, so a later rule of either kind wins exactly once.
pub fn resolve_rule_for<'a>(
    rules: &'a [ScheduleRule],
    connector: Option<&str>,
    at: NaiveDateTime,
) -> Result<Option<&'a ScheduleRule>, ConfigError> {
    let mut chosen = None;
    for rule in rules {
        let visible = match connector {
            Some(connector) => rule.connector.is_empty() || rule.connector == connector,
            None => rule.connector.is_empty(),
        };
        if visible && matches(rule, at)? {
            chosen = Some(rule);
        }
    }
    Ok(chosen)
}

/// Resolve the last matching rule aimed at exactly this connector.
///
/// This is the scope of a display whose own playlist beats global rules:
/// untargeted rules are invisible to it, while rules naming the connector keep
/// their authored last-match-wins order among themselves.
pub fn resolve_targeted_rule<'a>(
    rules: &'a [ScheduleRule],
    connector: &str,
    at: NaiveDateTime,
) -> Result<Option<&'a ScheduleRule>, ConfigError> {
    let mut chosen = None;
    for rule in rules {
        if !rule.connector.is_empty() && rule.connector == connector && matches(rule, at)? {
            chosen = Some(rule);
        }
    }
    Ok(chosen)
}

/// How far ahead `next_change` looks. Eight days covers every weekday
/// pattern; a change that only a month rule brings, further away than that,
/// is reported as none.
pub const CHANGE_HORIZON_DAYS: u64 = 8;

/// The local clock's readings over the search horizon, in the order they
/// will happen: runs of one UTC offset each. A clocks-forward change ends one
/// run and starts the next at a later reading (the readings between never
/// show); a clocks-back change starts the next run at an earlier reading,
/// so the repeated hour is read twice.
#[derive(Clone, Debug)]
pub struct Timeline {
    /// The local reading the search starts from.
    reading: NaiveDateTime,
    /// The real instant of `reading`, as UTC.
    start: NaiveDateTime,
    segments: Vec<Segment>,
}

#[derive(Clone, Debug)]
struct Segment {
    /// The first local reading of this run.
    from: NaiveDateTime,
    /// The reading this run would reach next but never shows (exclusive).
    until: NaiveDateTime,
    /// Local time minus UTC throughout the run.
    offset: TimeDelta,
}

/// One schedule change: the local time the clock will show, and the real
/// time until then.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Change {
    pub local: NaiveDateTime,
    pub after: TimeDelta,
}

impl Timeline {
    /// A clock without daylight-saving changes, starting at `reading`.
    pub fn fixed(reading: NaiveDateTime) -> Self {
        let until = reading
            .checked_add_days(Days::new(CHANGE_HORIZON_DAYS))
            .and_then(|horizon| horizon.checked_add_signed(TimeDelta::seconds(1)))
            .unwrap_or(NaiveDateTime::MAX);
        Self {
            reading,
            start: reading,
            segments: vec![Segment {
                from: reading,
                until,
                offset: TimeDelta::zero(),
            }],
        }
    }

    /// `zone`'s clock from the local `reading`. When `real` (the real clock
    /// now) reads `reading`, it is the start, which also says which pass of a
    /// repeated hour this is; otherwise (a caller driving time by hand) the
    /// earliest instant that reads `reading` is.
    pub fn from_reading<Tz: TimeZone>(
        reading: NaiveDateTime,
        real: DateTime<Utc>,
        zone: &Tz,
    ) -> Self {
        let now = real.with_timezone(zone).naive_local();
        let start = if (now - reading).abs() <= TimeDelta::seconds(5) {
            Some(real)
        } else {
            // chrono orders an ambiguous reading by offset, not by instant
            // (its `earliest` is standard time, the later one), so compare.
            match zone.from_local_datetime(&reading) {
                LocalResult::Single(instant) => Some(instant.with_timezone(&Utc)),
                LocalResult::Ambiguous(one, other) => {
                    Some(one.with_timezone(&Utc).min(other.with_timezone(&Utc)))
                }
                LocalResult::None => None,
            }
        };
        match start {
            Some(start) => Self::new(reading, start, zone),
            None => Self::fixed(reading),
        }
    }

    /// `zone`'s clock from `start`, which reads `reading`. Offset changes are
    /// found by sampling every six hours and bisecting to the second.
    pub fn new<Tz: TimeZone>(reading: NaiveDateTime, start: DateTime<Utc>, zone: &Tz) -> Self {
        let offset_at = |instant: NaiveDateTime| {
            TimeDelta::seconds(i64::from(
                zone.offset_from_utc_datetime(&instant)
                    .fix()
                    .local_minus_utc(),
            ))
        };
        let begin = start.naive_utc();
        let Some(end) = begin.checked_add_days(Days::new(CHANGE_HORIZON_DAYS)) else {
            return Self::fixed(reading);
        };
        let mut segments = Vec::new();
        let mut run_start = begin;
        let mut offset = offset_at(begin);
        let mut sample = begin;
        while sample < end {
            let next = (sample + TimeDelta::hours(6)).min(end);
            let next_offset = offset_at(next);
            if next_offset != offset {
                // Offsets change on whole seconds: bisect over them, keeping
                // `before` on the old offset and `after` on the new one.
                let at_second = |second: i64| {
                    DateTime::from_timestamp(second, 0).map_or(next, |instant| instant.naive_utc())
                };
                let mut before = sample.and_utc().timestamp();
                let mut after = next.and_utc().timestamp() + 1;
                while after - before > 1 {
                    let middle = before + (after - before) / 2;
                    if offset_at(at_second(middle)) == offset {
                        before = middle;
                    } else {
                        after = middle;
                    }
                }
                let after = at_second(after);
                segments.push(Segment {
                    from: run_start + offset,
                    until: after + offset,
                    offset,
                });
                run_start = after;
                offset = next_offset;
            }
            sample = next;
        }
        segments.push(Segment {
            from: run_start + offset,
            until: end + offset + TimeDelta::seconds(1),
            offset,
        });
        segments[0].from = reading;
        Self {
            reading,
            start: begin,
            segments,
        }
    }

    /// The local reading the search starts from.
    pub fn reading(&self) -> NaiveDateTime {
        self.reading
    }
}

/// The first change after the timeline's reading, at most
/// `CHANGE_HORIZON_DAYS` ahead: the first reading, in the order the clock
/// shows them, at which `decide` gives something other than it gives now.
///
/// A rule's match only changes at midnight (a new weekday or month) and at its
/// own start and end minutes; a wrapped window's after-midnight tail belongs
/// to its start day, so midnight is no edge for it. The clock's own jumps are
/// the other edges. `decide` is therefore asked at those readings within each
/// run of the timeline, and at the reading each later run starts from:
///
/// - after clocks go forward, 03:00 stands for every boundary in the skipped
///   02:00–02:59, so a window wholly inside the gap changes nothing;
/// - after clocks go back, 01:00 is judged as the clock rewinds to it, and the
///   repeated hour's boundaries are judged again as it passes them a second
///   time.
///
/// Without an enabled rule nothing can change.
///
/// Each `decide` is charged `rules.len()` against `budget`, the rule checks a
/// caller allows for all its searches together. When the budget runs out the
/// search stops and reports no change, so hundreds of rules on dozens of
/// displays cannot stall a status reply.
pub fn next_change<T: PartialEq, E>(
    rules: &[ScheduleRule],
    timeline: &Timeline,
    budget: &mut usize,
    mut decide: impl FnMut(NaiveDateTime) -> Result<T, E>,
) -> Result<Option<Change>, E> {
    if !rules.iter().any(|rule| rule.enabled) {
        return Ok(None);
    }
    let cost = rules.len();
    let mut decide = move |instant: NaiveDateTime| -> Result<Option<T>, E> {
        if *budget < cost {
            return Ok(None);
        }
        *budget -= cost;
        decide(instant).map(Some)
    };
    let mut minutes = vec![0u16];
    for rule in rules.iter().filter(|rule| rule.enabled) {
        for time in [&rule.start, &rule.end].into_iter().flatten() {
            if let Ok(minute) = parse_time(time) {
                minutes.push(minute);
            }
        }
    }
    minutes.sort_unstable();
    minutes.dedup();
    let Some(current) = decide(timeline.reading)? else {
        return Ok(None);
    };
    for (index, segment) in timeline.segments.iter().enumerate() {
        let change = |local: NaiveDateTime| Change {
            local,
            after: local - segment.offset - timeline.start,
        };
        if index > 0 {
            // The clock has just jumped to this reading.
            match decide(segment.from)? {
                None => return Ok(None),
                Some(answer) if answer != current => return Ok(Some(change(segment.from))),
                Some(_) => {}
            }
        }
        let mut day = segment.from.date();
        'run: loop {
            for minute in &minutes {
                let Some(candidate) =
                    day.and_hms_opt(u32::from(*minute / 60), u32::from(*minute % 60), 0)
                else {
                    continue;
                };
                if candidate <= segment.from {
                    continue;
                }
                if candidate >= segment.until {
                    break 'run;
                }
                match decide(candidate)? {
                    None => return Ok(None),
                    Some(answer) if answer != current => return Ok(Some(change(candidate))),
                    Some(_) => {}
                }
            }
            match day.succ_opt() {
                Some(next) => day = next,
                None => break,
            }
        }
    }
    Ok(None)
}

pub fn matches(rule: &ScheduleRule, at: NaiveDateTime) -> Result<bool, ConfigError> {
    if !rule.enabled {
        return Ok(false);
    }
    let minute = (at.hour() * 60 + at.minute()) as u16;
    let (within, wrapped_tail) = match (&rule.start, &rule.end) {
        (None, None) => Ok((true, false)),
        (Some(start), Some(end)) => {
            let start = parse_time(start)?;
            let end = parse_time(end)?;
            if start == end {
                Ok((true, false))
            } else if start < end {
                Ok((start <= minute && minute < end, false))
            } else {
                Ok((minute >= start || minute < end, minute < end))
            }
        }
        _ => Ok((false, false)),
    }?;
    if !within {
        return Ok(false);
    }

    // The after-midnight tail belongs to the day on which the wrapped window
    // started. This preserves the predecessor's `Mon 22:00-06:00` meaning and
    // its month boundary semantics instead of cutting it off at midnight.
    let calendar_at = if wrapped_tail {
        at.checked_sub_days(Days::new(1)).ok_or_else(|| {
            ConfigError::Invalid("wrapped schedule starts before the supported calendar".into())
        })?
    } else {
        at
    };
    if !rule.months.is_empty() && !rule.months.contains(&(calendar_at.month() as u8)) {
        return Ok(false);
    }
    let weekday = calendar_at.weekday().num_days_from_monday() as u8;
    Ok(rule.weekdays.is_empty() || rule.weekdays.contains(&weekday))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    fn at(y: i32, m: u32, d: u32, h: u32, n: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(h, n, 0)
            .unwrap()
    }
    fn rule(p: &str, s: Option<&str>, e: Option<&str>) -> ScheduleRule {
        ScheduleRule {
            id: p.into(),
            playlist: p.into(),
            connector: String::new(),
            months: vec![],
            weekdays: vec![],
            start: s.map(str::to_owned),
            end: e.map(str::to_owned),
            enabled: true,
        }
    }
    #[test]
    fn end_exclusive() {
        let r = vec![
            rule("am", Some("06:00"), Some("12:00")),
            rule("pm", Some("12:00"), Some("18:00")),
        ];
        assert_eq!(resolve(&r, "d", at(2026, 8, 3, 12, 0)).unwrap(), "pm");
    }
    #[test]
    fn wraps_midnight() {
        let r = vec![rule("night", Some("22:00"), Some("06:00"))];
        assert_eq!(resolve(&r, "day", at(2026, 8, 3, 23, 0)).unwrap(), "night");
        assert_eq!(resolve(&r, "day", at(2026, 8, 4, 6, 0)).unwrap(), "day");
    }

    #[test]
    fn wrapped_tail_uses_the_start_day_and_month() {
        let mut r = rule("monday-night", Some("22:00"), Some("06:00"));
        r.weekdays = vec![0];
        r.months = vec![8];
        assert!(matches(&r, at(2026, 8, 3, 23, 59)).unwrap());
        assert!(matches(&r, at(2026, 8, 4, 0, 0)).unwrap());
        assert!(matches(&r, at(2026, 8, 4, 5, 59)).unwrap());
        assert!(!matches(&r, at(2026, 8, 4, 6, 0)).unwrap());
        assert!(!matches(&r, at(2026, 8, 4, 23, 0)).unwrap());

        let mut december = rule("new-year", Some("22:00"), Some("06:00"));
        december.months = vec![12];
        assert!(matches(&december, at(2027, 1, 1, 2, 0)).unwrap());
        assert!(!matches(&december, at(2027, 1, 2, 2, 0)).unwrap());
    }
    #[test]
    fn last_wins() {
        let mut a = rule("weekday", None, None);
        a.weekdays = vec![0];
        let mut b = rule("august", None, None);
        b.months = vec![8];
        assert_eq!(
            resolve(&[a, b], "d", at(2026, 8, 3, 10, 0)).unwrap(),
            "august"
        );
    }
    #[test]
    fn clock_injectable() {
        struct Fixed(NaiveDateTime);
        impl Clock for Fixed {
            fn now(&self) -> NaiveDateTime {
                self.0
            }
        }
        let c = Fixed(at(2030, 12, 25, 3, 15));
        assert_eq!(c.now(), at(2030, 12, 25, 3, 15));
    }

    fn next_winner(rules: &[ScheduleRule], from: NaiveDateTime) -> Option<NaiveDateTime> {
        let mut unlimited = usize::MAX;
        next_change(rules, &Timeline::fixed(from), &mut unlimited, |instant| {
            resolve(rules, "d", instant)
        })
        .unwrap()
        .map(|change| change.local)
    }

    #[test]
    fn next_change_stops_when_its_budget_runs_out() {
        let r = vec![
            rule("am", Some("06:00"), Some("12:00")),
            rule("pm", Some("12:00"), Some("18:00")),
        ];
        let from = at(2026, 8, 3, 19, 0);
        let mut calls = 0;
        let mut count = |instant| {
            calls += 1;
            resolve(&r, "d", instant)
        };
        // 19:00 now, midnight, then 06:00 tomorrow: three decisions of two
        // rules each.
        let timeline = Timeline::fixed(from);
        let mut budget = 6;
        assert_eq!(
            next_change(&r, &timeline, &mut budget, &mut count).unwrap(),
            Some(Change {
                local: at(2026, 8, 4, 6, 0),
                after: TimeDelta::hours(11),
            })
        );
        assert_eq!(budget, 0);
        let mut budget = 5;
        assert_eq!(
            next_change(&r, &timeline, &mut budget, &mut count).unwrap(),
            None
        );
        assert_eq!(budget, 1);
        assert_eq!(calls, 5);
    }

    #[test]
    fn next_change_is_the_rule_boundary_that_changes_the_winner() {
        let r = vec![
            rule("am", Some("06:00"), Some("12:00")),
            rule("pm", Some("12:00"), Some("18:00")),
        ];
        assert_eq!(
            next_winner(&r, at(2026, 8, 3, 10, 0)),
            Some(at(2026, 8, 3, 12, 0))
        );
        assert_eq!(
            next_winner(&r, at(2026, 8, 3, 12, 0)),
            Some(at(2026, 8, 3, 18, 0))
        );
        // After the last window the next change is tomorrow's first one.
        assert_eq!(
            next_winner(&r, at(2026, 8, 3, 19, 0)),
            Some(at(2026, 8, 4, 6, 0))
        );
        // Seconds before a boundary still name the boundary minute.
        let almost = at(2026, 8, 3, 17, 59) + chrono::Duration::seconds(30);
        assert_eq!(next_winner(&r, almost), Some(at(2026, 8, 3, 18, 0)));
    }

    #[test]
    fn next_change_skips_a_handover_to_the_same_answer() {
        // Two windows choosing the same playlist meet at 12:00: nothing a
        // caller compares changes there, so the next change is 18:00.
        let mut late = rule("pm", Some("12:00"), Some("18:00"));
        late.playlist = "am".into();
        let r = vec![rule("am", Some("06:00"), Some("12:00")), late];
        assert_eq!(
            next_winner(&r, at(2026, 8, 3, 10, 0)),
            Some(at(2026, 8, 3, 18, 0))
        );
    }

    #[test]
    fn next_change_crosses_midnight_for_weekday_and_wrapped_rules() {
        let mut monday = rule("monday", None, None);
        monday.weekdays = vec![0];
        // 2026-08-03 is a Monday: the all-day rule ends at midnight.
        assert_eq!(
            next_winner(&[monday.clone()], at(2026, 8, 3, 23, 0)),
            Some(at(2026, 8, 4, 0, 0))
        );
        // On Tuesday the next change is the following Monday's midnight.
        assert_eq!(
            next_winner(&[monday], at(2026, 8, 4, 9, 0)),
            Some(at(2026, 8, 10, 0, 0))
        );
        // A wrapped window is no edge at midnight; it ends at 06:00.
        let night = vec![rule("night", Some("22:00"), Some("06:00"))];
        assert_eq!(
            next_winner(&night, at(2026, 8, 3, 23, 0)),
            Some(at(2026, 8, 4, 6, 0))
        );
        assert_eq!(
            next_winner(&night, at(2026, 8, 4, 7, 0)),
            Some(at(2026, 8, 4, 22, 0))
        );
    }

    #[test]
    fn next_change_is_bounded_and_needs_an_enabled_rule() {
        let mut december = rule("december", None, None);
        december.months = vec![12];
        // Months away: beyond the horizon, so none.
        assert_eq!(next_winner(&[december.clone()], at(2026, 8, 3, 9, 0)), None);
        // Inside the horizon: the first of the month.
        assert_eq!(
            next_winner(&[december.clone()], at(2026, 11, 28, 9, 0)),
            Some(at(2026, 12, 1, 0, 0))
        );
        december.enabled = false;
        assert_eq!(next_winner(&[december], at(2026, 11, 28, 9, 0)), None);
        assert_eq!(next_winner(&[], at(2026, 11, 28, 9, 0)), None);
        // A rule that always matches never changes.
        assert_eq!(
            next_winner(&[rule("always", None, None)], at(2026, 8, 3, 9, 0)),
            None
        );
    }

    /// The next change on Chicago's clock from the real instant `utc` (whose
    /// local reading is `reading`), as (local reading, real minutes until).
    fn chicago_change(
        rules: &[ScheduleRule],
        reading: NaiveDateTime,
        utc: NaiveDateTime,
    ) -> Option<(NaiveDateTime, i64)> {
        use crate::schedule::test_zones::Chicago2026;
        let timeline = Timeline::new(reading, utc.and_utc(), &Chicago2026);
        let mut unlimited = usize::MAX;
        next_change(rules, &timeline, &mut unlimited, |instant| {
            resolve(rules, "d", instant)
        })
        .unwrap()
        .map(|change| (change.local, change.after.num_minutes()))
    }

    #[test]
    fn clocks_going_back_are_a_boundary_and_repeat_the_hours_boundaries() {
        // 2026-11-01: at 02:00 CDT (07:00 UTC) clocks go back to 01:00 CST.
        let window = vec![rule("window", Some("01:30"), Some("02:30"))];
        // At the first 01:55 the window matches; five minutes later the clock
        // reads 01:00 again and it no longer does. Not 02:30.
        assert_eq!(
            chicago_change(&window, at(2026, 11, 1, 1, 55), at(2026, 11, 1, 6, 55)),
            Some((at(2026, 11, 1, 1, 0), 5))
        );
        // A window that still matches at 01:00 is no change there: it ends at
        // 02:30 standard time, 95 real minutes on.
        let long = vec![rule("long", Some("00:30"), Some("02:30"))];
        assert_eq!(
            chicago_change(&long, at(2026, 11, 1, 1, 55), at(2026, 11, 1, 6, 55)),
            Some((at(2026, 11, 1, 2, 30), 95))
        );
        // A boundary already passed in the first 01:00-01:59 comes round again.
        let short = vec![rule("short", Some("01:20"), Some("01:40"))];
        assert_eq!(
            chicago_change(&short, at(2026, 11, 1, 1, 50), at(2026, 11, 1, 6, 50)),
            Some((at(2026, 11, 1, 1, 20), 30))
        );
        // In the second pass there is no rewind left: 01:30 standard time.
        assert_eq!(
            chicago_change(&window, at(2026, 11, 1, 1, 10), at(2026, 11, 1, 7, 10)),
            Some((at(2026, 11, 1, 1, 30), 20))
        );
        // Before the repeated hour, its first 01:30 comes first.
        assert_eq!(
            chicago_change(&window, at(2026, 11, 1, 0, 30), at(2026, 11, 1, 5, 30)),
            Some((at(2026, 11, 1, 1, 30), 60))
        );
    }

    #[test]
    fn clocks_going_forward_judge_the_skipped_hour_at_its_end() {
        // 2026-03-08: at 02:00 CST (08:00 UTC) clocks go to 03:00 CDT.
        let late = vec![rule("late", Some("02:30"), Some("06:00"))];
        assert_eq!(
            chicago_change(&late, at(2026, 3, 8, 1, 0), at(2026, 3, 8, 7, 0)),
            Some((at(2026, 3, 8, 3, 0), 60))
        );
        // A window wholly inside the gap never shows that day.
        let lost = vec![rule("lost", Some("02:15"), Some("02:45"))];
        assert_eq!(
            chicago_change(&lost, at(2026, 3, 8, 1, 0), at(2026, 3, 8, 7, 0)),
            Some((at(2026, 3, 9, 2, 15), 24 * 60 + 15))
        );
    }

    #[test]
    fn a_reading_is_placed_by_the_real_clock_when_it_is_now() {
        use crate::schedule::test_zones::Chicago2026;
        let window = vec![rule("window", Some("01:30"), Some("02:30"))];
        let next = |timeline: &Timeline| {
            let mut unlimited = usize::MAX;
            next_change(&window, timeline, &mut unlimited, |instant| {
                resolve(&window, "d", instant)
            })
            .unwrap()
            .map(|change| change.local)
        };
        let reading = at(2026, 11, 1, 1, 55);
        // The real clock reads 01:55 standard time: the second pass.
        let second =
            Timeline::from_reading(reading, at(2026, 11, 1, 7, 55).and_utc(), &Chicago2026);
        assert_eq!(next(&second), Some(at(2026, 11, 1, 2, 30)));
        // A reading far from the real clock is its earliest instant: the first
        // pass, which rewinds.
        let driven = Timeline::from_reading(reading, at(2026, 8, 3, 12, 0).and_utc(), &Chicago2026);
        assert_eq!(next(&driven), Some(at(2026, 11, 1, 1, 0)));
        // A reading that never shows has no instant and no zone.
        let gap = Timeline::from_reading(
            at(2026, 3, 8, 2, 30),
            at(2026, 8, 3, 12, 0).and_utc(),
            &Chicago2026,
        );
        assert_eq!(gap.segments.len(), 1);
    }

    #[test]
    fn connector_scope_combines_global_and_targeted_rules_in_authored_order() {
        let global_first = rule("global-first", None, None);
        let mut dp = rule("dp", None, None);
        dp.connector = "DP-1".into();
        let global_last = rule("global-last", None, None);
        let rules = [global_first, dp, global_last];

        assert_eq!(
            resolve_rule_for(&rules, Some("DP-1"), at(2026, 8, 3, 10, 0))
                .unwrap()
                .unwrap()
                .playlist,
            "global-last"
        );
        assert_eq!(
            resolve_rule_for(&rules[..2], Some("DP-1"), at(2026, 8, 3, 10, 0))
                .unwrap()
                .unwrap()
                .playlist,
            "dp"
        );
        assert_eq!(
            resolve_rule_for(&rules[..2], Some("HDMI-A-1"), at(2026, 8, 3, 10, 0))
                .unwrap()
                .unwrap()
                .playlist,
            "global-first"
        );
        assert_eq!(
            resolve_rule(&rules[..2], at(2026, 8, 3, 10, 0))
                .unwrap()
                .unwrap()
                .playlist,
            "global-first",
            "mirrored resolution must ignore targeted rules"
        );
    }

    #[test]
    fn targeted_scope_ignores_global_rules_and_keeps_authored_order() {
        let global_first = rule("global-first", None, None);
        let mut dp_early = rule("dp-early", None, None);
        dp_early.connector = "DP-1".into();
        let mut dp_late = rule("dp-late", None, None);
        dp_late.connector = "DP-1".into();
        let mut dp_off = rule("dp-off", None, None);
        dp_off.connector = "DP-1".into();
        dp_off.enabled = false;
        let mut hdmi = rule("hdmi", None, None);
        hdmi.connector = "HDMI-A-1".into();
        let global_last = rule("global-last", None, None);
        let now = at(2026, 8, 3, 10, 0);

        let rules = [
            global_first.clone(),
            dp_early.clone(),
            dp_late,
            dp_off,
            hdmi,
            global_last.clone(),
        ];
        assert_eq!(
            resolve_targeted_rule(&rules, "DP-1", now)
                .unwrap()
                .unwrap()
                .id,
            "dp-late",
            "a later global rule must not beat a targeted one in this scope"
        );
        assert_eq!(
            resolve_rule_for(&rules, Some("DP-1"), now)
                .unwrap()
                .unwrap()
                .id,
            "global-last",
            "the established connector scope is unchanged"
        );
        assert!(
            resolve_targeted_rule(&[global_first, global_last], "DP-1", now)
                .unwrap()
                .is_none()
        );
        let mut evening = dp_early;
        evening.start = Some("18:00".into());
        evening.end = Some("22:00".into());
        assert!(
            resolve_targeted_rule(std::slice::from_ref(&evening), "DP-1", now)
                .unwrap()
                .is_none()
        );
        assert!(
            resolve_targeted_rule(&[evening], "DP-1", at(2026, 8, 3, 19, 0))
                .unwrap()
                .is_some()
        );
    }
}

/// A hand-written America/Chicago for 2026, so DST tests need no zone
/// database or process-wide `TZ`: CST (UTC-6) until 2026-03-08 08:00 UTC, CDT
/// (UTC-5) until 2026-11-01 07:00 UTC, then CST again.
#[cfg(test)]
pub(crate) mod test_zones {
    use chrono::{FixedOffset, LocalResult, NaiveDate, NaiveDateTime, TimeZone};

    #[derive(Clone, Copy, Debug)]
    pub(crate) struct Chicago2026;

    fn cst() -> FixedOffset {
        FixedOffset::west_opt(6 * 3600).unwrap()
    }

    fn cdt() -> FixedOffset {
        FixedOffset::west_opt(5 * 3600).unwrap()
    }

    fn utc(month: u32, day: u32, hour: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(2026, month, day)
            .unwrap()
            .and_hms_opt(hour, 0, 0)
            .unwrap()
    }

    impl TimeZone for Chicago2026 {
        type Offset = FixedOffset;

        fn from_offset(_offset: &FixedOffset) -> Self {
            Self
        }

        fn offset_from_utc_datetime(&self, utc_time: &NaiveDateTime) -> FixedOffset {
            if (utc(3, 8, 8)..utc(11, 1, 7)).contains(utc_time) {
                cdt()
            } else {
                cst()
            }
        }

        fn offset_from_utc_date(&self, utc_date: &NaiveDate) -> FixedOffset {
            self.offset_from_utc_datetime(&utc_date.and_hms_opt(0, 0, 0).unwrap())
        }

        fn offset_from_local_datetime(&self, local: &NaiveDateTime) -> LocalResult<FixedOffset> {
            // Each reading is valid when the instant it names has that offset.
            let valid = |offset: FixedOffset| {
                let instant = *local - offset;
                (self.offset_from_utc_datetime(&instant) == offset).then_some(offset)
            };
            match (valid(cdt()), valid(cst())) {
                // Standard time first, as chrono's own `Local` orders it
                // (by offset, so the later instant comes first).
                (Some(daylight), Some(standard)) => LocalResult::Ambiguous(standard, daylight),
                (Some(offset), None) | (None, Some(offset)) => LocalResult::Single(offset),
                (None, None) => LocalResult::None,
            }
        }

        fn offset_from_local_date(&self, local: &NaiveDate) -> LocalResult<FixedOffset> {
            self.offset_from_local_datetime(&local.and_hms_opt(12, 0, 0).unwrap())
        }
    }
}
