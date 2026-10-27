# memwatch

SwiftBar menu-bar plugin (single Python 3 file, stdlib only) that watches memory pressure on macOS.

- Shows swap usage in the menu bar (green < 6 GB, yellow >= 6 GB, red >= 10 GB) and sends a notification on yellow/red (re-notify every 30 min).
- Dropdown: memory per group (Claude, MCP, Chrome, VS Code, Dev, Docker, apps). Sizes come from `top` MEM (includes compressed memory); falls back to RSS.
- "CPU-Fresser": processes above 50 % instantaneous CPU (second `top` sample; kernel_task excluded).
- "Verwaiste Dev-Prozesse": node/npm/pnpm/vite/... with ppid 1 and running >= 1 h. "Beenden" kills the whole process tree (TERM, 2 s, then KILL) after re-checking the PID is still an orphan; "Alle verwaisten beenden" does this for all.

## Install

```sh
brew install --cask swiftbar          # set the plugin directory on first start
ln -s "$PWD/memwatch.30s.py" "$HOME/Library/Application Support/SwiftBar/Plugins/memwatch.30s.py"
osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:false}'
```

Thresholds are constants at the top of `memwatch.30s.py`. Kill mode: `memwatch.30s.py kill <pid>` / `killall`.
