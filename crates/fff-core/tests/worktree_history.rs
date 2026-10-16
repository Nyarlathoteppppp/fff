use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use fff_search::file_picker::FilePicker;
use fff_search::frecency::FrecencyTracker;
use fff_search::query_tracker::QueryTracker;
use fff_search::{
    FFFMode, FilePickerOptions, FuzzySearchOptions, PaginationArgs, QueryParser, SharedFilePicker,
    SharedFrecency,
};
use tempfile::TempDir;

#[test]
fn linked_worktree_reuses_and_feeds_the_main_checkout_history() {
    let fixture = Fixture::new();
    let main_hot = fixture.main.join("src/hot.rs");

    let tracker = FrecencyTracker::open(fixture.tmp.path().join("frecency")).unwrap();
    for _ in 0..3 {
        tracker.track_access(&main_hot).unwrap();
    }
    let frecency = SharedFrecency::default();
    frecency.init(tracker).unwrap();

    let shared = start_picker(&fixture.worktree, &frecency, false);
    let guard = shared.read().unwrap();
    let picker = guard.as_ref().unwrap();

    assert_eq!(
        picker.shared_base_path().as_os_str(),
        fixture.main.as_os_str(),
        "query history is keyed by this exact string"
    );
    assert_eq!(access_score(picker, &fixture.worktree, "src/hot.rs"), 3);
    assert_eq!(access_score(picker, &fixture.worktree, "src/cold.rs"), 0);
    drop(guard);

    // An open inside the worktree is mirrored into the main checkout history
    // and weighs double in the worktree that made it.
    let worktree_cold = fixture.worktree.join("src/cold.rs");
    let mut guard = shared.write().unwrap();
    let picker = guard.as_mut().unwrap();
    let shared_cold = picker.shared_path_for(&worktree_cold);
    assert_eq!(shared_cold, Some(fixture.main.join("src/cold.rs")));

    let frecency_guard = frecency.read().unwrap();
    let tracker = frecency_guard.as_ref().unwrap();
    tracker
        .track_access_shared(&worktree_cold, shared_cold.as_deref())
        .unwrap();
    picker
        .update_single_file_frecency(&worktree_cold, tracker)
        .unwrap();

    assert_eq!(access_score(picker, &fixture.worktree, "src/cold.rs"), 2);
    assert_eq!(
        tracker
            .access_count(&fixture.main.join("src/cold.rs"))
            .unwrap(),
        1
    );
}

#[test]
fn linked_worktree_shares_query_combo_boosts() {
    let fixture = Fixture::new();
    let mut queries = QueryTracker::open(fixture.tmp.path().join("queries")).unwrap();
    for _ in 0..3 {
        queries
            .track_query_completion("rs", &fixture.main, &fixture.main.join("src/cold.rs"))
            .unwrap();
    }

    let shared = start_picker(&fixture.worktree, &SharedFrecency::default(), false);
    let guard = shared.read().unwrap();
    let picker = guard.as_ref().unwrap();

    let parser = QueryParser::default();
    let query = parser.parse("rs");
    let results = picker.fuzzy_search(
        &query,
        Some(&queries),
        FuzzySearchOptions {
            project_path: Some(picker.shared_base_path()),
            combo_boost_score_multiplier: 100,
            min_combo_count: 3,
            pagination: PaginationArgs {
                offset: 0,
                limit: 10,
            },
            ..Default::default()
        },
    );

    assert_eq!(results.items[0].relative_path(picker), "src/cold.rs");
    assert!(results.scores[0].combo_match_boost > 0);
}

#[test]
fn main_checkout_keeps_its_own_history_keys() {
    let fixture = Fixture::new();
    let shared = start_picker(&fixture.main, &SharedFrecency::default(), false);
    let guard = shared.read().unwrap();
    let picker = guard.as_ref().unwrap();

    assert_eq!(picker.shared_base_path(), fixture.main.as_path());
    assert_eq!(
        picker.shared_path_for(fixture.main.join("src/hot.rs")),
        None
    );
}

// A linked worktree's `.git` is a file: index and HEAD live in
// `.git/worktrees/<name>` of the main checkout and still have to be watched.
#[test]
fn staging_inside_a_linked_worktree_refreshes_git_status() {
    let fixture = Fixture::new();
    let shared = start_picker(&fixture.worktree, &SharedFrecency::default(), true);
    assert!(
        shared.wait_for_watcher(Duration::from_secs(20)),
        "watcher timed out"
    );

    fs::write(fixture.worktree.join("src/hot.rs"), "changed\n").unwrap();
    wait_for_status(&shared, &fixture.worktree, "src/hot.rs", |status| {
        status.contains(git2::Status::WT_MODIFIED)
    });

    git(&fixture.worktree, &["add", "src/hot.rs"]);
    wait_for_status(&shared, &fixture.worktree, "src/hot.rs", |status| {
        status == git2::Status::INDEX_MODIFIED
    });
}

struct Fixture {
    tmp: TempDir,
    main: PathBuf,
    worktree: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let tmp = TempDir::new().unwrap();
        let root = fff_search::path_utils::canonicalize(tmp.path()).unwrap();
        let main = root.join("main");
        let worktree = root.join("feature");

        for file in ["src/hot.rs", "src/cold.rs"] {
            let path = main.join(file);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "seed\n").unwrap();
        }
        git(&main, &["init", "-b", "main"]);
        git(&main, &["add", "-A"]);
        git(&main, &["commit", "-m", "seed", "--no-gpg-sign"]);
        git(
            &main,
            &[
                "worktree",
                "add",
                worktree.to_str().unwrap(),
                "-b",
                "feature",
            ],
        );

        Self {
            tmp,
            main,
            worktree,
        }
    }
}

fn start_picker(base: &Path, frecency: &SharedFrecency, watch: bool) -> SharedFilePicker {
    let shared = SharedFilePicker::default();
    FilePicker::new_with_shared_state(
        shared.clone(),
        frecency.clone(),
        FilePickerOptions {
            base_path: base.to_string_lossy().into_owned(),
            mode: FFFMode::Neovim,
            watch,
            ..Default::default()
        },
    )
    .unwrap();

    assert!(
        shared.wait_for_scan(Duration::from_secs(20)),
        "scan timed out"
    );
    shared
}

fn access_score(picker: &FilePicker, base: &Path, relative_path: &str) -> i16 {
    picker
        .get_file_by_path(base.join(relative_path))
        .map(|file| file.access_frecency_score)
        .unwrap_or_else(|| panic!("{relative_path} not indexed"))
}

fn wait_for_status(
    shared: &SharedFilePicker,
    base: &Path,
    relative_path: &str,
    matches: impl Fn(git2::Status) -> bool,
) {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let status = shared.read().unwrap().as_ref().and_then(|picker| {
            picker
                .get_file_by_path(base.join(relative_path))
                .and_then(|file| file.git_status)
        });
        if status.is_some_and(&matches) {
            return;
        }

        assert!(
            Instant::now() < deadline,
            "timed out waiting for git status of {relative_path}, last seen {status:?}"
        );
        std::thread::sleep(Duration::from_millis(25));
    }
}

fn git(dir: &Path, args: &[&str]) {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}
