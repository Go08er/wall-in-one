//! Display discovery over the Wayland wire, against a compositor of the
//! test's own (`support::compositor`): with and without
//! wlr-output-management, across hot-plug, and with a service that starts
//! before its compositor or outlives one.

mod support;

use std::fs;
use std::path::PathBuf;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use support::compositor::{FakeCompositor, FakeHead, FakeOutput, Layout, WedgedCompositor};
use wall_in_one_service::outputs::{Output, OutputSource, WaylandOutputs};

/// A socket path in a fresh directory, short enough for a Unix socket.
fn socket(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .subsec_nanos();
    let directory =
        std::env::temp_dir().join(format!("wio-{label}-{}-{nonce}", std::process::id()));
    fs::create_dir_all(&directory).unwrap();
    directory.join("wayland-0")
}

fn laptop() -> FakeOutput {
    FakeOutput {
        make: "BOE".into(),
        model: "0x0A9B".into(),
        scale: 2,
        physical_mm: (340, 210),
        ..FakeOutput::new("eDP-1", (2560, 1600, 165_004), (0, 0, 1706, 1066))
    }
}

fn monitor() -> FakeOutput {
    FakeOutput {
        make: "Dell Inc.".into(),
        model: "DELL U2720Q".into(),
        ..FakeOutput::new("DP-1", (3840, 2160, 59_997), (1706, 0, 3840, 2160))
    }
}

fn names(outputs: &[Output]) -> Vec<(&str, bool)> {
    outputs
        .iter()
        .map(|output| (output.name.as_str(), output.enabled))
        .collect()
}

#[test]
fn outputs_come_from_wl_output_and_xdg_output_alone() {
    let path = socket("plain");
    let _compositor = FakeCompositor::start(&path, Layout::of(vec![laptop(), monitor()]));
    let mut source = WaylandOutputs::at(path.clone());

    let found = source.outputs().unwrap();

    assert_eq!(
        found,
        vec![
            Output {
                name: "DP-1".into(),
                enabled: true,
                make: "Dell Inc.".into(),
                model: "DELL U2720Q".into(),
                x: 1706,
                y: 0,
                width: 3840,
                height: 2160,
                scale: 1.0,
                mode_width: 3840,
                mode_height: 2160,
                refresh_millihz: Some(59_997),
                physical_width_mm: 600,
                physical_height_mm: 340,
            },
            Output {
                name: "eDP-1".into(),
                enabled: true,
                make: "BOE".into(),
                model: "0x0A9B".into(),
                x: 0,
                y: 0,
                width: 1706,
                height: 1066,
                // The logical size says 1.5, whatever whole-number buffer
                // scale wl_output rounds it to.
                scale: 1.5,
                mode_width: 2560,
                mode_height: 1600,
                refresh_millihz: Some(165_004),
                physical_width_mm: 340,
                physical_height_mm: 210,
            },
        ]
    );
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn xdg_output_version_2_ends_its_own_batch() {
    // Weston 16 offers zxdg_output_manager_v1 version 2.
    let path = socket("xdg2");
    let layout = Layout {
        xdg_output_version: Some(2),
        ..Layout::of(vec![laptop()])
    };
    let _compositor = FakeCompositor::start(&path, layout);

    let found = WaylandOutputs::at(path.clone()).outputs().unwrap();

    assert_eq!(names(&found), [("eDP-1", true)]);
    assert_eq!((found[0].width, found[0].height), (1706, 1066));
    assert_eq!(found[0].scale, 1.5);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn without_xdg_output_the_mode_and_whole_scale_still_come_through() {
    let path = socket("noxdg");
    let layout = Layout {
        xdg_output_version: None,
        ..Layout::of(vec![laptop()])
    };
    let _compositor = FakeCompositor::start(&path, layout);

    let found = WaylandOutputs::at(path.clone()).outputs().unwrap();

    assert_eq!(names(&found), [("eDP-1", true)]);
    assert_eq!((found[0].mode_width, found[0].mode_height), (2560, 1600));
    assert_eq!((found[0].width, found[0].height), (0, 0));
    assert_eq!(found[0].scale, 2.0);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn an_output_older_than_wl_output_version_4_is_named_by_xdg_output() {
    let path = socket("v3");
    let layout = Layout {
        wl_output_version: 3,
        xdg_output_version: Some(2),
        ..Layout::of(vec![laptop(), monitor()])
    };
    let _compositor = FakeCompositor::start(&path, layout);

    let found = WaylandOutputs::at(path.clone()).outputs().unwrap();

    assert_eq!(names(&found), [("DP-1", true), ("eDP-1", true)]);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn wlr_output_management_adds_switched_off_heads_and_the_exact_scale() {
    let path = socket("wlr");
    let mut enabled = FakeHead::new("eDP-1", true);
    enabled.make = "BOE Technology".into();
    enabled.scale = 1.25;
    enabled.mode = (2560, 1600, 165_004);
    let mut off = FakeHead::new("HDMI-A-1", false);
    off.physical_mm = (700, 390);
    let layout = Layout {
        heads: Some(vec![enabled, off]),
        ..Layout::of(vec![laptop()])
    };
    let _compositor = FakeCompositor::start(&path, layout);

    let found = WaylandOutputs::at(path.clone()).outputs().unwrap();

    assert_eq!(names(&found), [("HDMI-A-1", false), ("eDP-1", true)]);
    let laptop = &found[1];
    assert_eq!(laptop.scale, 1.25, "the compositor's own scale wins");
    assert_eq!(laptop.make, "BOE Technology");
    assert_eq!(laptop.mode_width, 2560);
    let switched_off = &found[0];
    assert_eq!(
        (
            switched_off.physical_width_mm,
            switched_off.physical_height_mm
        ),
        (700, 390)
    );
    assert_eq!(
        switched_off.refresh_millihz, None,
        "no current mode while off"
    );
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn hot_plug_is_seen_on_the_next_question_over_the_same_connection() {
    let path = socket("plug");
    let compositor = FakeCompositor::start(&path, Layout::of(vec![laptop()]));
    let mut source = WaylandOutputs::at(path.clone());
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);

    compositor.set_layout(Layout::of(vec![laptop(), monitor()]));
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("DP-1", true), ("eDP-1", true)]
    );

    compositor.set_layout(Layout::of(vec![monitor()]));
    let found = source.outputs().unwrap();
    assert_eq!(names(&found), [("DP-1", true)]);
    assert_eq!(found[0].x, 1706, "the remaining output keeps what it said");

    assert!(source.connected());
    assert_eq!(compositor.connections(), 1, "one connection for all three");
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_head_switched_off_and_unplugged_follows_wlr_output_management() {
    let path = socket("heads");
    let both = |hdmi_enabled: bool| Layout {
        heads: Some(vec![
            FakeHead::new("eDP-1", true),
            FakeHead::new("HDMI-A-1", hdmi_enabled),
        ]),
        ..Layout::of(if hdmi_enabled {
            vec![
                laptop(),
                FakeOutput::new("HDMI-A-1", (1920, 1080, 60_000), (1706, 0, 1920, 1080)),
            ]
        } else {
            vec![laptop()]
        })
    };
    let compositor = FakeCompositor::start(&path, both(true));
    let mut source = WaylandOutputs::at(path.clone());
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("HDMI-A-1", true), ("eDP-1", true)]
    );

    compositor.set_layout(both(false));
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("HDMI-A-1", false), ("eDP-1", true)]
    );

    compositor.set_layout(Layout {
        heads: Some(vec![FakeHead::new("eDP-1", true)]),
        ..Layout::of(vec![laptop()])
    });
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_service_started_before_its_compositor_connects_once_it_is_there() {
    let path = socket("late");
    let mut source = WaylandOutputs::at(path.clone());

    let error = source.outputs().unwrap_err();
    assert!(
        error.starts_with("cannot connect to the Wayland compositor"),
        "{error}"
    );
    assert!(!source.connected());

    let _compositor = FakeCompositor::start(&path, Layout::of(vec![laptop()]));
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_compositor_that_restarts_is_connected_to_again() {
    let path = socket("restart");
    let first = FakeCompositor::start(&path, Layout::of(vec![laptop()]));
    let mut source = WaylandOutputs::at(path.clone());
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);

    drop(first);
    let error = source.outputs().unwrap_err();
    assert!(
        error.starts_with("cannot connect to the Wayland compositor"),
        "{error}"
    );

    let second = FakeCompositor::start(&path, Layout::of(vec![monitor()]));
    assert_eq!(names(&source.outputs().unwrap()), [("DP-1", true)]);
    assert_eq!(second.connections(), 1);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn more_outputs_than_routes_are_refused_not_truncated() {
    let path = socket("crowd");
    let crowd = (0..65)
        .map(|index| {
            FakeOutput::new(
                &format!("DP-{index}"),
                (1920, 1080, 60_000),
                (0, 0, 1920, 1080),
            )
        })
        .collect();
    let _compositor = FakeCompositor::start(&path, Layout::of(crowd));

    let error = WaylandOutputs::at(path.clone()).outputs().unwrap_err();

    assert!(
        error.contains("more than the supported 64 outputs"),
        "{error}"
    );
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_connector_name_no_renderer_could_be_given_is_left_out() {
    let path = socket("names");
    let _compositor = FakeCompositor::start(
        &path,
        Layout::of(vec![
            laptop(),
            FakeOutput::new("Display Port 1", (1920, 1080, 60_000), (0, 0, 1920, 1080)),
        ]),
    );

    let found = WaylandOutputs::at(path.clone()).outputs().unwrap();

    assert_eq!(names(&found), [("eDP-1", true)]);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_compositor_that_never_answers_is_given_up_on_at_the_deadline() {
    let path = socket("silent");
    let _compositor = FakeCompositor::start_silent(&path);
    let started = Instant::now();

    let error = WaylandOutputs::at(path.clone()).outputs().unwrap_err();

    assert!(error.contains("did not answer within"), "{error}");
    let waited = started.elapsed();
    assert!(waited >= Duration::from_millis(2900), "{waited:?}");
    assert!(waited < Duration::from_secs(6), "{waited:?}");
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn every_question_to_a_compositor_that_stopped_accepting_ends_at_its_deadline() {
    // The first connection fills the stopped compositor's queue; every later
    // one finds it full. Asked on a thread, so a question that never ends
    // fails this test instead of hanging it.
    let path = socket("wedged");
    let _compositor = WedgedCompositor::start(&path);
    let (sender, receiver) = mpsc::channel();
    let asked = path.clone();
    thread::spawn(move || {
        let mut source = WaylandOutputs::at(asked);
        for _ in 0..3 {
            let started = Instant::now();
            let result = source.outputs();
            if sender.send((started.elapsed(), result)).is_err() {
                return;
            }
        }
    });
    for question in 0..3 {
        let Ok((waited, result)) = receiver.recv_timeout(Duration::from_secs(10)) else {
            panic!("question {question} to a stopped compositor did not end within 10 s");
        };
        let error = result.unwrap_err();
        assert!(
            error.contains("did not answer within") || error.contains("did not accept"),
            "question {question}: {error}"
        );
        if question > 0 {
            // The queue is full from the first question on: the connect is
            // what times out, and it claims no duration it may not have had.
            assert!(
                error.contains("did not accept the connection in the time the question had left"),
                "question {question}: {error}"
            );
        }
        assert!(
            waited < Duration::from_secs(4),
            "question {question} took {waited:?}"
        );
    }
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_finished_manager_lets_its_heads_go() {
    let path = socket("finished");
    let compositor = FakeCompositor::start(
        &path,
        Layout {
            heads: Some(vec![
                FakeHead::new("eDP-1", true),
                FakeHead::new("HDMI-A-1", false),
            ]),
            ..Layout::of(vec![laptop()])
        },
    );
    let mut source = WaylandOutputs::at(path.clone());
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("HDMI-A-1", false), ("eDP-1", true)]
    );

    // The compositor withdraws wlr-output-management: the manager finishes,
    // its heads without events of their own.
    compositor.set_layout(Layout::of(vec![laptop()]));
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);
    // The releases go out with the next question's requests.
    source.outputs().unwrap();
    assert_eq!(compositor.released_heads(), 2, "both heads released");
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

/// eDP-1 on, HDMI-A-1 off and only known to wlr-output-management; eDP-1
/// announcing `extra_modes` more modes than it has.
fn flooding(extra_modes: usize) -> Layout {
    Layout {
        heads: Some(vec![
            FakeHead {
                extra_modes,
                ..FakeHead::new("eDP-1", true)
            },
            FakeHead::new("HDMI-A-1", false),
        ]),
        ..Layout::of(vec![laptop()])
    }
}

#[test]
fn wlr_output_management_left_out_after_a_flood_comes_back_with_the_next_compositor() {
    let path = socket("flood-then-sane");
    let mut source = WaylandOutputs::at(path.clone());

    let flood = FakeCompositor::start(&path, flooding(5_000));
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);
    // Left out for as long as that compositor runs: no reconnecting per question.
    assert_eq!(names(&source.outputs().unwrap()), [("eDP-1", true)]);
    assert_eq!(
        flood.connections(),
        2,
        "the flooded connection and its replacement"
    );
    drop(flood);
    assert!(source.outputs().is_err(), "that compositor is gone");

    // The compositor restarted, or another one took the socket.
    let sane = FakeCompositor::start(&path, flooding(0));
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("HDMI-A-1", false), ("eDP-1", true)],
        "the switched-off head is listed again"
    );
    drop(sane);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn a_flooding_compositor_that_also_times_out_is_flooded_once() {
    let path = socket("flood-slow");
    let mut source = WaylandOutputs::at(path.clone());
    let flood = FakeCompositor::start(
        &path,
        Layout {
            slow_without_wlr: true,
            ..flooding(5_000)
        },
    );
    // The first question floods and its second connection times out; the
    // next ones time out without wlr-output-management. A timeout is no sign
    // that the compositor went away, so none brings the protocol back.
    for question in 0..3 {
        let started = Instant::now();
        assert!(
            source.outputs().is_err(),
            "question {question} was answered"
        );
        let took = started.elapsed();
        assert!(
            took < Duration::from_millis(3_100),
            "question {question} took {took:?}"
        );
    }
    assert_eq!(flood.wlr_binds(), 1, "flooded by the first question only");
    drop(flood);

    // Another compositor in its place, found without a question failing in
    // between: a new socket, so the protocol is back.
    let sane = FakeCompositor::start(&path, flooding(0));
    assert_eq!(
        names(&source.outputs().unwrap()),
        [("HDMI-A-1", false), ("eDP-1", true)],
        "the switched-off head is listed again"
    );
    drop(sane);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
}
