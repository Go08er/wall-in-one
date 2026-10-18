//! Runtime-only half of Wall-in-One.
//!
//! This crate deliberately has no Python binding and no knowledge of the
//! application's library files. Its sole configuration input is a resolved,
//! versioned TOML document.

pub mod config;
pub mod outputs;
pub mod power;
pub mod protocol;
pub mod renderer;
pub mod runtime;
pub mod schedule;

/// The install kinds a build can be, in the order `docs/packaging.md` lists
/// them: this flake's packages, a distribution package built from source,
/// and anything else (the checked-in default).
pub const INSTALL_KINDS: [&str; 3] = ["nix", "system", "source"];

/// What kind of install this build is, fixed when it is compiled.
///
/// A package sets `WALL_IN_ONE_INSTALL_KIND` while cargo builds the service;
/// unset, it is `source`. The Python half carries the same value in
/// `wall_in_one.install_kind`, which the package rewrites, and
/// `tools/check-install-kind.sh` checks that both say the same. Reported by
/// `--version` and in `status` as `install_kind`, which is how the companion
/// tells a Nix install from a distribution's.
pub const INSTALL_KIND: &str = install_kind(option_env!("WALL_IN_ONE_INSTALL_KIND"));

/// The install kind a build-time value names, `source` for none. Anything
/// but one of `INSTALL_KINDS` is refused, which in `INSTALL_KIND` is a
/// compile error: a misspelt kind must not ship as a fourth one.
pub const fn install_kind(configured: Option<&'static str>) -> &'static str {
    match configured {
        None => "source",
        Some(kind) if is_install_kind(kind) => kind,
        Some(_) => panic!("WALL_IN_ONE_INSTALL_KIND must be nix, system or source"),
    }
}

/// Whether `kind` is one of `INSTALL_KINDS`.
pub const fn is_install_kind(kind: &str) -> bool {
    let mut index = 0;
    while index < INSTALL_KINDS.len() {
        if same_bytes(kind.as_bytes(), INSTALL_KINDS[index].as_bytes()) {
            return true;
        }
        index += 1;
    }
    false
}

const fn same_bytes(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut index = 0;
    while index < left.len() {
        if left[index] != right[index] {
            return false;
        }
        index += 1;
    }
    true
}

#[cfg(test)]
mod install_kind_tests {
    use super::*;

    #[test]
    fn unset_is_a_source_build() {
        assert_eq!(install_kind(None), "source");
    }

    #[test]
    fn each_known_kind_is_kept() {
        for kind in INSTALL_KINDS {
            assert_eq!(install_kind(Some(kind)), kind);
        }
    }

    #[test]
    fn anything_else_is_refused() {
        for kind in ["", "Nix", "nix ", "arch", "sourc", "systemd"] {
            assert!(!is_install_kind(kind), "{kind:?}");
            assert!(std::panic::catch_unwind(|| install_kind(Some(kind))).is_err());
        }
    }

    #[test]
    fn this_build_is_one_of_them() {
        assert!(is_install_kind(INSTALL_KIND), "{INSTALL_KIND}");
        assert_eq!(
            INSTALL_KIND,
            option_env!("WALL_IN_ONE_INSTALL_KIND").unwrap_or("source")
        );
    }
}
