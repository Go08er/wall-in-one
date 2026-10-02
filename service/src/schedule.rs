use crate::config::{ConfigError, ScheduleRule, parse_time};
use chrono::{Datelike, Days, LocalResult, NaiveDateTime, TimeDelta, TimeZone, Timelike};

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

/// The local time a clock in `zone` shows when it reaches `local`: `local`
/// itself, or, for a time inside a clocks-forward gap that never appears, the
/// first minute after the gap. The service reads its schedule from that
/// clock, so on the spring change a boundary at 02:30 acts at 03:00.
pub fn shown_on<Tz: TimeZone>(local: NaiveDateTime, zone: &Tz) -> NaiveDateTime {
    let mut probe = local;
    // No zone skips more than a day.
    for _ in 0..24 * 60 {
        if !matches!(zone.from_local_datetime(&probe), LocalResult::None) {
            return probe;
        }
        let Some(next) = probe.checked_add_signed(TimeDelta::minutes(1)) else {
            break;
        };
        probe = next;
    }
    local
}

/// The first whole minute after `at`, at most `CHANGE_HORIZON_DAYS` ahead, at
/// which `decide` gives something other than what it gives at `at`.
///
/// A rule's match only changes at midnight (a new weekday or month) and at its
/// own start and end minutes; a wrapped window's after-midnight tail belongs
/// to its start day, so midnight is no edge for it. `decide` is therefore
/// asked only at those instants. Without an enabled rule nothing can change.
///
/// `shown` maps each of those instants to the time the local clock will show
/// when it gets there (`shown_on`): a boundary inside a clocks-forward gap is
/// judged, and reported, at the gap's end, so a window that lies entirely
/// inside the gap changes nothing. In the repeated hour after clocks go back
/// the local clock passes the same times twice; the first one is reported.
///
/// Each `decide` is charged `rules.len()` against `budget`, the rule checks a
/// caller allows for all its searches together. When the budget runs out the
/// search stops and reports no change, so hundreds of rules on dozens of
/// displays cannot stall a status reply.
pub fn next_change<T: PartialEq, E>(
    rules: &[ScheduleRule],
    at: NaiveDateTime,
    budget: &mut usize,
    mut shown: impl FnMut(NaiveDateTime) -> NaiveDateTime,
    mut decide: impl FnMut(NaiveDateTime) -> Result<T, E>,
) -> Result<Option<NaiveDateTime>, E> {
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
    let Some(current) = decide(at)? else {
        return Ok(None);
    };
    let horizon = at
        .checked_add_days(Days::new(CHANGE_HORIZON_DAYS))
        .unwrap_or(NaiveDateTime::MAX);
    let mut judged = None;
    for offset in 0..=CHANGE_HORIZON_DAYS {
        let Some(day) = at.date().checked_add_days(Days::new(offset)) else {
            break;
        };
        for minute in &minutes {
            let Some(candidate) =
                day.and_hms_opt(u32::from(*minute / 60), u32::from(*minute % 60), 0)
            else {
                continue;
            };
            // Gap times map forward, so the order is kept; several can land
            // on the gap's end, which needs judging once.
            let candidate = shown(candidate);
            if candidate <= at || judged == Some(candidate) {
                continue;
            }
            if candidate > horizon {
                return Ok(None);
            }
            judged = Some(candidate);
            match decide(candidate)? {
                None => return Ok(None),
                Some(answer) if answer != current => return Ok(Some(candidate)),
                Some(_) => {}
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
        next_change(
            rules,
            from,
            &mut unlimited,
            |instant| instant,
            |instant| resolve(rules, "d", instant),
        )
        .unwrap()
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
        let mut budget = 6;
        assert_eq!(
            next_change(&r, from, &mut budget, |instant| instant, &mut count).unwrap(),
            Some(at(2026, 8, 4, 6, 0))
        );
        assert_eq!(budget, 0);
        let mut budget = 5;
        assert_eq!(
            next_change(&r, from, &mut budget, |instant| instant, &mut count).unwrap(),
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

    #[test]
    fn a_boundary_inside_the_spring_gap_acts_when_the_clock_reaches_its_end() {
        use crate::schedule::test_zones::Chicago2026;
        let chicago = |instant| shown_on(instant, &Chicago2026);
        let next = |rules: &[ScheduleRule], from| {
            let mut unlimited = usize::MAX;
            next_change(rules, from, &mut unlimited, chicago, |instant| {
                resolve(rules, "d", instant)
            })
            .unwrap()
        };
        // 2026-03-08: clocks go from 02:00 CST to 03:00 CDT.
        assert_eq!(
            shown_on(at(2026, 3, 8, 2, 30), &Chicago2026),
            at(2026, 3, 8, 3, 0)
        );
        assert_eq!(
            shown_on(at(2026, 3, 8, 1, 59), &Chicago2026),
            at(2026, 3, 8, 1, 59)
        );
        let late = vec![rule("late", Some("02:30"), Some("06:00"))];
        assert_eq!(
            next(&late, at(2026, 3, 8, 1, 0)),
            Some(at(2026, 3, 8, 3, 0))
        );
        // A window entirely inside the gap never shows that day.
        let lost = vec![rule("lost", Some("02:15"), Some("02:45"))];
        assert_eq!(
            next(&lost, at(2026, 3, 8, 1, 0)),
            Some(at(2026, 3, 9, 2, 15))
        );
        // On the autumn change the repeated hour exists; nothing moves.
        assert_eq!(
            shown_on(at(2026, 11, 1, 1, 30), &Chicago2026),
            at(2026, 11, 1, 1, 30)
        );
        let early = vec![rule("early", Some("01:30"), Some("06:00"))];
        assert_eq!(
            next(&early, at(2026, 11, 1, 0, 30)),
            Some(at(2026, 11, 1, 1, 30))
        );
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
                (Some(daylight), Some(standard)) => LocalResult::Ambiguous(daylight, standard),
                (Some(offset), None) | (None, Some(offset)) => LocalResult::Single(offset),
                (None, None) => LocalResult::None,
            }
        }

        fn offset_from_local_date(&self, local: &NaiveDate) -> LocalResult<FixedOffset> {
            self.offset_from_local_datetime(&local.and_hms_opt(12, 0, 0).unwrap())
        }
    }
}
