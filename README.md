# memwatch

A macOS menu-bar monitor that tells you **when your Mac is running out of memory and what is eating it**.
Two Python files (stdlib only), run as [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin.

Built for developer machines with limited RAM that run many things at once: dev servers, test runners,
several AI coding sessions, browsers. On those, the failure mode is slow creep: swap fills up, everything
lags, and the cause is a pile of forgotten processes.

```
🔴 11.6 GB                       ← menu bar: swap in use (🟢 < 6 GB, 🟡 ≥ 6 GB, 🔴 ≥ 10 GB)
─────────────────────────────
Swap 11.6 GB (warn 6, kritisch 10) · frei 60 %
macOS-System: 2436 MB       ▸    ← per group, top 5 processes in the submenu
VS Code: 1671 MB            ▸
Chrome: 1553 MB             ▸
Dev (node/npm/vite): 238 MB ▸
─────────────────────────────
CPU-Fresser (1)                  ← processes above 50 % CPU right now
Verwaiste Dev-Prozesse (3)  ▸    ← orphaned dev processes, click to kill
   Alle verwaisten beenden
```

(The menu text is German.)

## What it does

- **Swap in the menu bar**, colour-coded, plus a macOS notification when it crosses a threshold
  (at most once every 30 min per level).
- **Memory by group**: Claude sessions, MCP servers, Chrome, VS Code, dev tooling, Docker/Colima, then every
  other app by name. Sizes come from `top`'s MEM column, which includes compressed memory, so processes
  that are partly swapped out are not under-reported (falls back to RSS).
- **CPU hogs**: anything above 50 % *instantaneous* CPU (second `top` sample, not the lifetime average
  that `ps` reports). This catches runaway test workers that look idle in a memory view.
- **Orphaned dev processes**: `node`, `npm`, `pnpm`, `vite`, `vitest`, `tsx`, `esbuild`, `tsc`, `eslint`,
  `turbo` or `python -m http.server` whose parent is gone (`ppid 1`) and that have run for over an hour.
  These are the leftovers of closed terminals and crashed agents.

## Disk usage (`memwatch-disk.30m.py`)

A read-only part that answers "where did my disk go?". It never deletes anything. It is a separate
script, but `memwatch.30s.py` runs it and appends its section to its own dropdown, so there is only
**one** menu-bar item (macOS hides a second item when the bar is full). Do not link it into the plugin
folder on its own.

- **Menu bar**: the title shows `💾` plus free GB on `/` next to the swap figure; the icon turns yellow
  below 20 GB free and red below 10 GB.
- **Git worktrees**: repos are discovered generically. Every direct subfolder of each entry in `ROOTS`
  (default: the home directory) that has a `.git` entry is asked for `git worktree list --porcelain`,
  and the results are de-duplicated. Per worktree: total size, `node_modules` size (root plus
  `apps/*`, `packages/*`, `services/*`), branch, age of the last commit, dirty (`git status --porcelain`
  non-empty) and unpushed (`git log @{u}..` non-empty, or no upstream). A worktree that is clean **and**
  fully pushed is marked `✓` as "removable". It is only a hint. Biggest first, top 15, with totals.
- **Big caches**: npm, pnpm, `~/Library/Caches`, `~/.cache`, Xcode DerivedData / DeviceSupport,
  CoreSimulator and Colima, when they exist. Nested paths are not counted twice.

### Never blocks SwiftBar

Walking dozens of `node_modules` trees takes minutes, far beyond what SwiftBar tolerates. The plugin
therefore only reads `~/Library/Caches/memwatch-disk.json` and prints it (well under 0.1 s). If the
cache is missing or older than `TTL_SECONDS` (30 min) and no refresh is running, it spawns a detached
`nice -n 19` background refresh (`--refresh`). A lock file prevents overlapping runs (stale after 1 h),
and the cache is written atomically (temp file plus rename). While data is missing the menu says
"Wird berechnet…"; otherwise it shows the age of the data. The menu also has a manual "Jetzt neu
berechnen" entry.

### Read-only and safe

Git is always called as `git -C <path> --no-optional-locks ...` and only with `worktree list`,
`status`, and `log`. Sizes come from `du -skP`, which does not follow symlinks.

### Caveat: `du` and hardlinks

Sizes are what `du` reports for the physical directory. `du` counts a hardlinked file once per call,
so if your package manager hardlinks `node_modules` from a shared store (pnpm's default), per-worktree
numbers overlap and their sum can exceed the real disk usage; deleting one worktree frees less than
shown. With hoisted/copied installs the numbers are real. Check with `stat -f '%l'` on a file in
`node_modules`: a link count above 1 means hardlinks.

## Killing is explicit and guarded

Nothing is ever killed automatically. Clicking *Beenden* (or *Alle verwaisten beenden*) runs
`memwatch.30s.py kill <pid>` / `killall`, which:

1. re-checks that the PID **still** has `ppid 1` and still matches the dev-process pattern
   (protects against PID reuse), and refuses otherwise;
2. sends `TERM` to the whole process **tree**, waits 2 s, then `KILL`s survivors.

Killing only the wrapper (`npm`, `pnpm`) is not enough: they ignore `TERM` and leave their children
running, which is why the tree is handled.

## Install

Requires macOS and the system `/usr/bin/python3` (3.9+).

```sh
brew install --cask swiftbar            # choose a plugin folder on first start
git clone https://github.com/Movm/memwatch.git
ln -s "$PWD/memwatch/memwatch.30s.py" "<your SwiftBar plugin folder>/memwatch.30s.py"
```

Optional, start SwiftBar at login:

```sh
osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:false}'
```

The `30s` in the file name is SwiftBar's refresh interval. One run takes about 1.5 s, dominated by the
two `top` samples.

## Configure

Edit the constants at the top of `memwatch.30s.py`:

| Constant | Default | Meaning |
|---|---|---|
| `WARN_GB` / `CRIT_GB` | 6 / 10 | swap thresholds for 🟡 / 🔴 |
| `CPU_MIN` | 50 | % CPU for the CPU-hog list |
| `ORPHAN_MIN_SECONDS` | 3600 | minimum age of an orphan |
| `RENOTIFY_SECONDS` | 1800 | notification repeat interval |
| `DEV`, `GROUPS` | – | regexes for orphan detection and memory groups |
| `DISK_WARN_GB` / `DISK_CRIT_GB` | 20 / 10 | free-disk colour thresholds (memory plugin) |

`memwatch-disk.30m.py`:

| Constant | Default | Meaning |
|---|---|---|
| `ROOTS` | `["~"]` | folders whose direct subfolders are scanned for repos |
| `WARN_FREE_GB` / `CRIT_FREE_GB` | 20 / 10 | free-disk colour thresholds |
| `TTL_SECONDS` | 1800 | max cache age before a background refresh starts |
| `TOP_WORKTREES` | 15 | worktrees listed in the menu |
| `CACHES` | see file | list of `(label, path)` caches to measure |

## Limitations

- **Orphan detection is a heuristic.** A dev server you deliberately started with `nohup` or via
  `launchd` also has `ppid 1` and will be listed. It is only killed if you click it.
- The memory groups and the notification texts are tuned for one setup and are German; change the
  regexes in `GROUPS` for yours.
- No Linux/Windows support: it relies on macOS `top`, `sysctl vm.swapusage`, `memory_pressure` and
  `osascript`.
- Swap thresholds are absolute GB, not relative to installed RAM.

## Uninstall

Remove the symlink from the plugin folder, delete the login item in System Settings → General → Login
Items, and `rm ~/Library/Caches/memwatch.state ~/Library/Caches/memwatch-disk.json`.

## License

MIT, see [LICENSE](LICENSE).
