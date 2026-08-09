//! Runtime-only half of Wall-in-One.
//!
//! This crate deliberately has no Python binding and no knowledge of the
//! application's library files. Its sole configuration input is a resolved,
//! versioned TOML document.

pub mod config;
pub mod protocol;
pub mod renderer;
pub mod runtime;
pub mod schedule;
