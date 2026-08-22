use crate::config::{parse_time, ConfigError, ScheduleRule};
use chrono::{Datelike, NaiveDateTime, Timelike};

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

pub fn matches(rule: &ScheduleRule, at: NaiveDateTime) -> Result<bool, ConfigError> {
    if !rule.enabled {
        return Ok(false);
    }
    if !rule.months.is_empty() && !rule.months.contains(&(at.month() as u8)) {
        return Ok(false);
    }
    let weekday = at.weekday().num_days_from_monday() as u8;
    if !rule.weekdays.is_empty() && !rule.weekdays.contains(&weekday) {
        return Ok(false);
    }
    let minute = (at.hour() * 60 + at.minute()) as u16;
    match (&rule.start, &rule.end) {
        (None, None) => Ok(true),
        (Some(start), Some(end)) => {
            let start = parse_time(start)?;
            let end = parse_time(end)?;
            if start == end {
                Ok(true)
            } else if start < end {
                Ok(start <= minute && minute < end)
            } else {
                Ok(minute >= start || minute < end)
            }
        }
        _ => Ok(false),
    }
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
}
