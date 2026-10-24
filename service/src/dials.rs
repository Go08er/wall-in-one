//! The renderer dials: global defaults in `[renderer]`, per-wallpaper
//! overrides on a playlist entry, and what each becomes on a renderer's
//! command line (docs/renderer-settings.md).
//!
//! Every value is typed and bounded when the document loads, and every
//! argument here is formatted from that type, never copied from text:
//! mpvpaper writes `-o` into a config file with each space a new line, so a
//! value carrying one would be a configuration line of its own.

use crate::config::{RendererSettings, SceneClamp, SceneScaling, VideoWhenHidden};
use serde::Deserialize;

pub const MIN_VIDEO_SPEED: f64 = 0.25;
pub const MAX_VIDEO_SPEED: f64 = 4.0;
/// mpv's equalizer range for brightness, contrast, saturation and gamma.
pub const MAX_VIDEO_EQUALIZER: i16 = 100;
pub const VIDEO_ROTATIONS: &[u16] = &[0, 90, 180, 270];
pub const MIN_SCENE_FPS: u16 = 1;
pub const MAX_SCENE_FPS: u16 = 240;
pub const MAX_SCENE_VOLUME: u8 = 100;

/// How a video meets the screen's shape.
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VideoFit {
    /// Cover the screen, cropping what doesn't fit (`panscan=1.0`, as always).
    #[default]
    Fill,
    /// Show it whole, with bars (`panscan=0.0`).
    Fit,
    /// Stretch it to the screen (`keepaspect=no`).
    Stretch,
}

/// mpv's `scale`: the upscaling filter. Default leaves mpv's own choice.
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
pub enum VideoScale {
    #[default]
    #[serde(rename = "")]
    Default,
    #[serde(rename = "bilinear")]
    Bilinear,
    #[serde(rename = "spline36")]
    Spline36,
    #[serde(rename = "lanczos")]
    Lanczos,
    #[serde(rename = "ewa_lanczos")]
    EwaLanczos,
    #[serde(rename = "mitchell")]
    Mitchell,
}

impl VideoScale {
    pub fn option(self) -> Option<&'static str> {
        match self {
            Self::Default => None,
            Self::Bilinear => Some("bilinear"),
            Self::Spline36 => Some("spline36"),
            Self::Lanczos => Some("lanczos"),
            Self::EwaLanczos => Some("ewa_lanczos"),
            Self::Mitchell => Some("mitchell"),
        }
    }
}

/// mpvpaper `--auto-mode`: what else besides being hidden pauses or stops it.
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
pub enum VideoHiddenTrigger {
    /// Only being hidden (mpvpaper's own default).
    #[default]
    #[serde(rename = "")]
    Hidden,
    /// Any fullscreen window.
    #[serde(rename = "full")]
    Full,
    /// Any fullscreen or maximized window.
    #[serde(rename = "max")]
    Max,
}

impl VideoHiddenTrigger {
    pub fn option(self) -> Option<&'static str> {
        match self {
            Self::Hidden => None,
            Self::Full => Some("full"),
            Self::Max => Some("max"),
        }
    }
}

/// One video wallpaper's own dials; `None` follows the global default.
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct VideoOverrides {
    #[serde(default)]
    pub fit: Option<VideoFit>,
    #[serde(default)]
    pub speed: Option<f64>,
    #[serde(default)]
    pub brightness: Option<i16>,
    #[serde(default)]
    pub contrast: Option<i16>,
    #[serde(default)]
    pub saturation: Option<i16>,
    #[serde(default)]
    pub gamma: Option<i16>,
    #[serde(default)]
    pub rotate: Option<u16>,
    #[serde(default)]
    pub deband: Option<bool>,
}

/// One scene's (or web wallpaper's) own dials; `None` follows the default.
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SceneOverrides {
    #[serde(default)]
    pub fps: Option<u16>,
    #[serde(default)]
    pub scaling: Option<SceneScaling>,
    #[serde(default)]
    pub clamp: Option<SceneClamp>,
    #[serde(default)]
    pub muted: Option<bool>,
    #[serde(default)]
    pub volume: Option<u8>,
    #[serde(default)]
    pub audio_processing: Option<bool>,
    #[serde(default)]
    pub particles: Option<bool>,
    #[serde(default)]
    pub mouse: Option<bool>,
    #[serde(default)]
    pub parallax: Option<bool>,
}

fn speed_ok(speed: f64) -> bool {
    speed.is_finite() && (MIN_VIDEO_SPEED..=MAX_VIDEO_SPEED).contains(&speed)
}

fn equalizer_ok(value: i16) -> bool {
    (-MAX_VIDEO_EQUALIZER..=MAX_VIDEO_EQUALIZER).contains(&value)
}

/// Why the global dials in `settings` are out of bounds, or `None`.
pub fn renderer_problem(settings: &RendererSettings) -> Option<String> {
    if !speed_ok(settings.video_speed) {
        return Some(format!(
            "video_speed must be between {MIN_VIDEO_SPEED} and {MAX_VIDEO_SPEED}"
        ));
    }
    for (name, value) in [
        ("video_brightness", settings.video_brightness),
        ("video_contrast", settings.video_contrast),
        ("video_saturation", settings.video_saturation),
        ("video_gamma", settings.video_gamma),
    ] {
        if !equalizer_ok(value) {
            return Some(format!("{name} must be between -100 and 100"));
        }
    }
    if !VIDEO_ROTATIONS.contains(&settings.video_rotate) {
        return Some("video_rotate must be 0, 90, 180 or 270".into());
    }
    None
}

/// Why one video's own dials are out of bounds, or `None`.
pub fn video_problem(own: &VideoOverrides) -> Option<String> {
    if own.speed.is_some_and(|speed| !speed_ok(speed)) {
        return Some(format!(
            "a video entry's speed must be between {MIN_VIDEO_SPEED} and {MAX_VIDEO_SPEED}"
        ));
    }
    if [own.brightness, own.contrast, own.saturation, own.gamma]
        .into_iter()
        .flatten()
        .any(|value| !equalizer_ok(value))
    {
        return Some(
            "a video entry's brightness, contrast, saturation and gamma must be between -100 and 100"
                .into(),
        );
    }
    if own
        .rotate
        .is_some_and(|rotate| !VIDEO_ROTATIONS.contains(&rotate))
    {
        return Some("a video entry's rotate must be 0, 90, 180 or 270".into());
    }
    None
}

/// Why one scene's own dials are out of bounds, or `None`.
pub fn scene_problem(own: &SceneOverrides) -> Option<String> {
    if own
        .fps
        .is_some_and(|fps| !(MIN_SCENE_FPS..=MAX_SCENE_FPS).contains(&fps))
    {
        return Some(format!(
            "a scene entry's fps must be between {MIN_SCENE_FPS} and {MAX_SCENE_FPS}"
        ));
    }
    if own.volume.is_some_and(|volume| volume > MAX_SCENE_VOLUME) {
        return Some(format!(
            "a scene entry's volume must be between 0 and {MAX_SCENE_VOLUME}"
        ));
    }
    None
}

/// Whether `settings` holds a global dial a schema-6 service doesn't know.
pub fn renderer_needs_schema_seven(settings: &RendererSettings) -> bool {
    settings.video_fit != VideoFit::Fill
        || settings.video_speed != 1.0
        || settings.video_brightness != 0
        || settings.video_contrast != 0
        || settings.video_saturation != 0
        || settings.video_gamma != 0
        || settings.video_rotate != 0
        || settings.video_deband
        || settings.video_scale != VideoScale::Default
        || settings.video_hidden_trigger != VideoHiddenTrigger::Hidden
        || settings.scene_pause_only_active
        || !settings.scene_automute
        || !settings.scene_audio_processing
        || !settings.scene_particles
        || !settings.scene_mouse
        || !settings.scene_parallax
}

/// mpv's `panscan` for a fit, and whether `keepaspect=no` goes with it.
fn fit_options(fit: VideoFit) -> (&'static str, bool) {
    match fit {
        VideoFit::Fill => ("panscan=1.0", false),
        VideoFit::Fit => ("panscan=0.0", false),
        VideoFit::Stretch => ("panscan=0.0", true),
    }
}

/// The `panscan=` option a video plays with: its own fit, else the default.
pub fn mpv_panscan(settings: &RendererSettings, own: Option<&VideoOverrides>) -> &'static str {
    let fit = own.and_then(|own| own.fit).unwrap_or(settings.video_fit);
    fit_options(fit).0
}

/// The mpv options a video's dials add, each only when it isn't mpv's
/// default, so a video nobody adjusted runs exactly as before.
pub fn mpv_options(settings: &RendererSettings, own: Option<&VideoOverrides>) -> Vec<String> {
    let blank = VideoOverrides::default();
    let own = own.unwrap_or(&blank);
    let mut options = Vec::new();
    let fit = own.fit.unwrap_or(settings.video_fit);
    if fit_options(fit).1 {
        options.push("keepaspect=no".to_string());
    }
    let speed = own.speed.unwrap_or(settings.video_speed);
    if speed != 1.0 {
        options.push(format!("speed={speed:.2}"));
    }
    for (name, value) in [
        (
            "brightness",
            own.brightness.unwrap_or(settings.video_brightness),
        ),
        ("contrast", own.contrast.unwrap_or(settings.video_contrast)),
        (
            "saturation",
            own.saturation.unwrap_or(settings.video_saturation),
        ),
        ("gamma", own.gamma.unwrap_or(settings.video_gamma)),
    ] {
        if value != 0 {
            options.push(format!("{name}={value}"));
        }
    }
    let rotate = own.rotate.unwrap_or(settings.video_rotate);
    if rotate != 0 {
        options.push(format!("video-rotate={rotate}"));
    }
    if own.deband.unwrap_or(settings.video_deband) {
        options.push("deband=yes".to_string());
    }
    if let Some(scale) = settings.video_scale.option() {
        options.push(format!("scale={scale}"));
    }
    options
}

/// mpvpaper's `--auto-mode` argument, when the global dial asks for one and
/// a video pauses or stops at all.
pub fn mpvpaper_auto_mode(settings: &RendererSettings) -> Option<&'static str> {
    match settings.video_when_hidden {
        VideoWhenHidden::Play => None,
        VideoWhenHidden::Pause | VideoWhenHidden::Stop => settings.video_hidden_trigger.option(),
    }
}

/// What a scene plays with: its own dials over the global defaults.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SceneDials {
    pub fps: u16,
    pub scaling: SceneScaling,
    pub clamp: SceneClamp,
    pub muted: bool,
    pub volume: u8,
    pub audio_processing: bool,
    pub particles: bool,
    pub mouse: bool,
    pub parallax: bool,
}

pub fn scene_dials(settings: &RendererSettings, own: Option<&SceneOverrides>) -> SceneDials {
    let blank = SceneOverrides::default();
    let own = own.unwrap_or(&blank);
    SceneDials {
        fps: own.fps.unwrap_or(settings.scene_fps),
        scaling: own.scaling.unwrap_or(settings.scene_scaling),
        clamp: own.clamp.unwrap_or(settings.scene_clamp),
        muted: own.muted.unwrap_or(settings.scene_muted),
        volume: own.volume.unwrap_or(settings.scene_volume),
        audio_processing: own
            .audio_processing
            .unwrap_or(settings.scene_audio_processing),
        particles: own.particles.unwrap_or(settings.scene_particles),
        mouse: own.mouse.unwrap_or(settings.scene_mouse),
        parallax: own.parallax.unwrap_or(settings.scene_parallax),
    }
}

/// linux-wallpaperengine's flags for `dials` and the global-only dials, each
/// only when it isn't the engine's default. `--silent` and `--volume` are a
/// mutually exclusive group there (ApplicationContext.cpp:518-528): exactly
/// one is given.
pub fn scene_flags(settings: &RendererSettings, dials: SceneDials) -> Vec<String> {
    let mut flags = Vec::new();
    if dials.muted {
        flags.push("--silent".to_string());
    } else {
        flags.push("--volume".to_string());
        flags.push(dials.volume.to_string());
    }
    if !settings.scene_pause_when_covered {
        flags.push("--no-fullscreen-pause".to_string());
    }
    if settings.scene_pause_only_active {
        flags.push("--fullscreen-pause-only-active".to_string());
    }
    if !settings.scene_automute {
        flags.push("--noautomute".to_string());
    }
    if !dials.audio_processing {
        flags.push("--no-audio-processing".to_string());
    }
    if !dials.particles {
        flags.push("--disable-particles".to_string());
    }
    if !dials.mouse {
        flags.push("--disable-mouse".to_string());
    }
    if !dials.parallax {
        flags.push("--disable-parallax".to_string());
    }
    flags
}

#[cfg(test)]
mod tests {
    use super::*;

    fn defaults() -> RendererSettings {
        toml::from_str(
            r#"noctalia_program = "/bin/true"
mpvpaper_program = "/bin/true"
linux_wallpaperengine_program = "/bin/true"
own_scene_renderer = true
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
"#,
        )
        .unwrap()
    }

    /// One mpv config-file line each, whatever the bounds allow: `name=value`
    /// with nothing in the value that mpvpaper would split or unquote.
    fn single_line(option: &str) -> bool {
        let Some((name, value)) = option.split_once('=') else {
            return false;
        };
        !name.is_empty()
            && name.bytes().all(|b| b.is_ascii_lowercase() || b == b'-')
            && !value.is_empty()
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".:_-".contains(&b))
    }

    #[test]
    fn untouched_dials_add_nothing() {
        let settings = defaults();
        assert!(!renderer_needs_schema_seven(&settings));
        assert!(mpv_options(&settings, None).is_empty());
        assert_eq!(mpv_panscan(&settings, None), "panscan=1.0");
        assert_eq!(mpvpaper_auto_mode(&settings), None);
        assert_eq!(
            scene_flags(&settings, scene_dials(&settings, None)),
            ["--silent"]
        );
    }

    #[test]
    fn every_bounded_mpv_value_is_one_config_line() {
        let mut settings = defaults();
        settings.video_scale = VideoScale::EwaLanczos;
        for fit in [VideoFit::Fill, VideoFit::Fit, VideoFit::Stretch] {
            for speed in [MIN_VIDEO_SPEED, 0.3, 1.0, 1.55, MAX_VIDEO_SPEED] {
                for eq in [-MAX_VIDEO_EQUALIZER, -1, 0, 7, MAX_VIDEO_EQUALIZER] {
                    for rotate in VIDEO_ROTATIONS {
                        let own = VideoOverrides {
                            fit: Some(fit),
                            speed: Some(speed),
                            brightness: Some(eq),
                            contrast: Some(eq),
                            saturation: Some(eq),
                            gamma: Some(eq),
                            rotate: Some(*rotate),
                            deband: Some(true),
                        };
                        assert!(video_problem(&own).is_none());
                        let mut options = mpv_options(&settings, Some(&own));
                        options.push(mpv_panscan(&settings, Some(&own)).into());
                        for option in options {
                            assert!(single_line(&option), "{option}");
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn an_override_wins_over_the_default_and_only_where_set() {
        let mut settings = defaults();
        settings.video_speed = 2.0;
        settings.video_brightness = 10;
        let own = VideoOverrides {
            speed: Some(0.5),
            fit: Some(VideoFit::Stretch),
            ..VideoOverrides::default()
        };
        assert_eq!(
            mpv_options(&settings, Some(&own)),
            ["keepaspect=no", "speed=0.50", "brightness=10"]
        );
        assert_eq!(mpv_panscan(&settings, Some(&own)), "panscan=0.0");
        assert_eq!(
            mpv_options(&settings, None),
            ["speed=2.00", "brightness=10"]
        );
    }

    #[test]
    fn out_of_bounds_dials_are_refused() {
        for own in [
            VideoOverrides {
                speed: Some(4.01),
                ..VideoOverrides::default()
            },
            VideoOverrides {
                speed: Some(f64::NAN),
                ..VideoOverrides::default()
            },
            VideoOverrides {
                gamma: Some(101),
                ..VideoOverrides::default()
            },
            VideoOverrides {
                rotate: Some(45),
                ..VideoOverrides::default()
            },
        ] {
            assert!(video_problem(&own).is_some(), "{own:?}");
        }
        for own in [
            SceneOverrides {
                fps: Some(0),
                ..SceneOverrides::default()
            },
            SceneOverrides {
                fps: Some(241),
                ..SceneOverrides::default()
            },
            SceneOverrides {
                volume: Some(101),
                ..SceneOverrides::default()
            },
        ] {
            assert!(scene_problem(&own).is_some(), "{own:?}");
        }
        let mut settings = defaults();
        settings.video_rotate = 1;
        assert!(renderer_problem(&settings).is_some());
    }

    #[test]
    fn scene_flags_follow_the_dials_and_volume_excludes_silent() {
        let mut settings = defaults();
        settings.scene_pause_only_active = true;
        settings.scene_automute = false;
        let own = SceneOverrides {
            muted: Some(false),
            volume: Some(40),
            audio_processing: Some(false),
            particles: Some(false),
            mouse: Some(false),
            parallax: Some(false),
            ..SceneOverrides::default()
        };
        let flags = scene_flags(&settings, scene_dials(&settings, Some(&own)));
        assert_eq!(
            flags,
            [
                "--volume",
                "40",
                "--fullscreen-pause-only-active",
                "--noautomute",
                "--no-audio-processing",
                "--disable-particles",
                "--disable-mouse",
                "--disable-parallax",
            ]
        );
        assert!(!flags.iter().any(|flag| flag == "--silent"));
    }
}
