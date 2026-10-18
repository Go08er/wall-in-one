//! What a compositor announces is bounded on the client side: a flood of
//! wlr-output-management heads or modes costs a session no more than the
//! ceilings allow, and the enabled outputs still come through over wl_output.
//!
//! A test crate of its own, because it counts every allocation the process
//! makes (`#[global_allocator]`).

mod support;

use std::alloc::{GlobalAlloc, Layout as AllocLayout, System};
use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::{AtomicIsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use support::compositor::{FakeCompositor, FakeHead, FakeOutput, Layout};
use wall_in_one_service::outputs::{Output, OutputSource, WaylandOutputs};

struct Counting;
static LIVE: AtomicIsize = AtomicIsize::new(0);
/// The measurements must not overlap: they read one process-wide count.
static ONE_AT_A_TIME: Mutex<()> = Mutex::new(());

// SAFETY: forwards every call to the system allocator unchanged and only
// counts the bytes it hands out and takes back.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: AllocLayout) -> *mut u8 {
        LIVE.fetch_add(layout.size() as isize, Ordering::SeqCst);
        unsafe { System.alloc(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: AllocLayout) {
        LIVE.fetch_sub(layout.size() as isize, Ordering::SeqCst);
        unsafe { System.dealloc(ptr, layout) }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: AllocLayout, new: usize) -> *mut u8 {
        LIVE.fetch_add(new as isize - layout.size() as isize, Ordering::SeqCst);
        unsafe { System.realloc(ptr, layout, new) }
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

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
    FakeOutput::new("eDP-1", (2560, 1600, 60_000), (0, 0, 2560, 1600))
}

/// What one question found, and the bytes the session it leaves behind holds
/// once the compositor itself is gone.
fn retained(layout: Layout) -> (Vec<Output>, isize) {
    let path = socket("limits");
    let compositor = FakeCompositor::start(&path, layout);
    let mut source = WaylandOutputs::at(path.clone());
    let found = source.outputs().unwrap();
    drop(compositor);
    let with_session = LIVE.load(Ordering::SeqCst);
    drop(source);
    let without = LIVE.load(Ordering::SeqCst);
    fs::remove_dir_all(path.parent().unwrap()).unwrap();
    (found, with_session - without)
}

fn names(found: &[Output]) -> Vec<(&str, bool)> {
    found
        .iter()
        .map(|output| (output.name.as_str(), output.enabled))
        .collect()
}

#[test]
fn a_flood_of_modes_is_bounded_and_the_outputs_still_come_through() {
    let _one = ONE_AT_A_TIME
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let with_heads = |extra_modes: usize| Layout {
        heads: Some(vec![
            FakeHead {
                extra_modes,
                ..FakeHead::new("eDP-1", true)
            },
            FakeHead::new("HDMI-A-1", false),
        ]),
        ..Layout::of(vec![laptop()])
    };
    let (ordinary, baseline) = retained(with_heads(0));
    assert_eq!(names(&ordinary), [("HDMI-A-1", false), ("eDP-1", true)]);

    let (flooded, kept) = retained(with_heads(50_000));

    assert!(
        kept < baseline + 1024 * 1024,
        "50 000 announced modes were kept: {kept} bytes against {baseline}"
    );
    // wl_output still names the display; the switched-off head, which only
    // the flooding protocol knew about, is no longer listed.
    assert_eq!(names(&flooded), [("eDP-1", true)]);
}

#[test]
fn a_flood_of_heads_from_a_manager_without_release_is_bounded() {
    let _one = ONE_AT_A_TIME
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    // Version 2 has no `release`: past the ceiling, the heads cannot be let
    // go one by one, only with the connection.
    let with_heads = |count: usize| Layout {
        heads: Some(
            (0..count)
                .map(|index| FakeHead::new(&format!("OFF-{index}"), false))
                .collect(),
        ),
        wlr_version: 2,
        ..Layout::of(vec![laptop()])
    };
    let (_, baseline) = retained(with_heads(1));

    let (flooded, kept) = retained(with_heads(20_000));

    assert!(
        kept < baseline + 1024 * 1024,
        "20 000 announced heads were kept: {kept} bytes against {baseline}"
    );
    assert_eq!(names(&flooded), [("eDP-1", true)]);
}
