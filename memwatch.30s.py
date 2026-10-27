#!/usr/bin/python3
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""Menüleisten-Anzeige: Swap/RAM, größte Verbraucher, CPU-Fresser, verwaiste Dev-Prozesse.

Aufruf mit Argument (Kill-Modus, aus dem Menü): `kill <pid>` oder `killall`.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict

WARN_GB = 6
CRIT_GB = 10
CPU_MIN = 50
DISK_WARN_GB = 20
DISK_CRIT_GB = 10
SELF = os.path.realpath(__file__)
ORPHAN_MIN_SECONDS = 3600
RENOTIFY_SECONDS = 1800
STATE = os.path.expanduser("~/Library/Caches/memwatch.state")

DEV = re.compile(r"\b(node|npm|pnpm|vite|vitest|tsx|esbuild|tsc|eslint|turbo)\b|http\.server")
GROUPS = [
    ("Claude-Sessions", re.compile(r"native-binary/claude|claude-code")),
    ("MCP-Server", re.compile(r"mcp|playwright")),
    ("Chrome", re.compile(r"Google Chrome")),
    ("VS Code", re.compile(r"Visual Studio Code|Code Helper")),
    ("Dev (node/npm/vite)", DEV),
    ("Docker/Colima", re.compile(r"limactl|colima|qemu|Virtualization|docker", re.I)),
]


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def to_seconds(etime):
    days, _, rest = etime.rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return (int(days or 0) * 24 + h) * 3600 + m * 60 + s


def swap_gb():
    m = re.search(r"used = ([\d.]+)M", run("sysctl", "-n", "vm.swapusage"))
    return float(m.group(1)) / 1024 if m else 0.0


def free_pct():
    m = re.search(r"free percentage: (\d+)%", run("memory_pressure"))
    return int(m.group(1)) if m else None


def processes():
    out = []
    for line in run("ps", "-Ao", "pid=,ppid=,etime=,rss=,command=").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, etime, rss, cmd = parts
        out.append((int(pid), int(ppid), to_seconds(etime), int(rss) // 1024, cmd))
    return out


def top_sample():
    """Zweites top-Sample: {pid: (cpu_pct, mem_mb)}. MEM enthält komprimierten Speicher."""
    out = run("top", "-l", "2", "-s", "1", "-stats", "pid,cpu,mem")
    last = out.rsplit("PID ", 1)[-1]  # nur das zweite Sample (das erste ist Lifetime-Schnitt)
    units = {"B": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024}
    res = {}
    for line in last.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+([\d.]+)\s+(\d+)([BKMG])", line)
        if m:
            res[int(m.group(1))] = (float(m.group(2)), float(m.group(3)) * units[m.group(4)])
    return res


def descendants(pid, procs):
    kids = defaultdict(list)
    for p in procs:
        kids[p[1]].append(p[0])
    found, stack = [], [pid]
    while stack:
        cur = stack.pop()
        for k in kids[cur]:
            found.append(k)
            stack.append(k)
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_tree(pid):
    """Wurzel + alle Nachkommen: TERM, 2 s warten, Überlebende KILL. Guard gegen PID-Reuse."""
    procs = processes()
    root = next((p for p in procs if p[0] == pid), None)
    if root is None:
        print(f"{pid}: läuft nicht mehr")
        return
    if root[1] != 1 or not DEV.search(root[4]):
        print(f"{pid}: abgelehnt (kein verwaister Dev-Prozess mehr)")
        return
    tree = [pid] + descendants(pid, procs)
    tree = [t for t in tree if t != os.getpid()]
    for t in tree:
        try:
            os.kill(t, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 2
    while time.time() < deadline and any(alive(t) for t in tree):
        time.sleep(0.1)
    for t in tree:
        if alive(t):
            try:
                os.kill(t, signal.SIGKILL)
            except OSError:
                pass
    print(f"{pid}: Baum mit {len(tree)} Prozessen beendet")


def find_orphans(procs):
    return [
        p for p in procs
        if p[1] == 1 and p[2] >= ORPHAN_MIN_SECONDS and DEV.search(p[4])
    ]


def group_of(cmd):
    for name, rx in GROUPS:
        if rx.search(cmd):
            return name
    app = re.search(r"/Applications/([^/]+)\.app", cmd)
    if app:
        return app.group(1)
    if cmd.startswith(("/System/", "/usr/", "/Library/")):
        return "macOS-System"
    return os.path.basename(cmd.split()[0])


def label(cmd):
    cmd = cmd.replace("|", "/")
    return cmd if len(cmd) <= 90 else cmd[:87] + "…"


def notify(level, swap, top):
    now = time.time()
    try:
        last_level, last_ts = open(STATE).read().split()
        last_ts = float(last_ts)
    except (OSError, ValueError):
        last_level, last_ts = "ok", 0.0
    if level == "ok":
        open(STATE, "w").write(f"ok {now}")
        return
    if level == last_level and now - last_ts < RENOTIFY_SECONDS:
        return
    open(STATE, "w").write(f"{level} {now}")
    title = "Mac-Speicher kritisch" if level == "crit" else "Mac-Speicher wird knapp"
    msg = f"Swap {swap:.1f} GB. Größte: {top}"
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'])


def main():
    swap = swap_gb()
    free = free_pct()
    level = "crit" if swap >= CRIT_GB else "warn" if swap >= WARN_GB else "ok"
    icon = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}[level]

    procs = processes()
    sample = top_sample()
    # Footprint-nahe Größe aus top (inkl. komprimiert); Fallback RSS
    procs = [(p[0], p[1], p[2], round(sample[p[0]][1]) if p[0] in sample else p[3], p[4]) for p in procs]
    groups = defaultdict(list)
    for p in procs:
        groups[group_of(p[4])].append(p)
    totals = sorted(
        ((sum(p[3] for p in ps), name) for name, ps in groups.items()), reverse=True
    )
    notify(level, swap, ", ".join(f"{n} {mb} MB" for mb, n in totals[:3]))

    orphans = find_orphans(procs)
    me = os.getpid()
    runaways = sorted(
        (
            (sample[p[0]][0], p) for p in procs
            if p[0] in sample and sample[p[0]][0] > CPU_MIN
            and p[0] not in (0, me) and not p[4].startswith(("top ", "/usr/bin/top"))
            and "kernel_task" not in p[4]
        ),
        key=lambda x: -x[0],
    )

    disk = shutil.disk_usage("/").free / 1024 ** 3
    if disk < DISK_CRIT_GB:
        icon = "🔴"
    elif disk < DISK_WARN_GB and icon == "🟢":
        icon = "🟡"
    print(f"{icon} {swap:.1f} GB · 💾 {disk:.0f}")
    print("---")
    free_txt = f" · frei {free} %" if free is not None else ""
    print(f"Swap {swap:.1f} GB (warn {WARN_GB}, kritisch {CRIT_GB}){free_txt}")
    print("---")
    for mb, name in totals:
        if mb < 50:
            continue
        print(f"{name}: {mb} MB")
        for p in sorted(groups[name], key=lambda x: -x[3])[:5]:
            print(f"--{p[3]} MB · {p[0]} · {label(p[4])} | font=Menlo size=11")
    print("---")
    if runaways:
        print(f"CPU-Fresser ({len(runaways)}) | color=red")
        for cpu, (pid, _, secs, _, cmd) in runaways:
            print(f"--{cpu:.0f} % · {pid} · {secs // 3600} h {secs % 3600 // 60} min · {label(cmd)} | font=Menlo size=11")
    print("---")
    if orphans:
        print(f"Verwaiste Dev-Prozesse ({len(orphans)}) | color=orange")
        for pid, _, secs, mb, cmd in sorted(orphans, key=lambda x: -x[2]):
            print(
                f"--Beenden {pid} · {secs // 3600} h · {label(cmd)}"
                f" | bash={SELF} param1=kill param2={pid} terminal=false refresh=true"
            )
        print(f"--Alle verwaisten beenden | bash={SELF} param1=killall terminal=false refresh=true")
    else:
        print("Keine verwaisten Dev-Prozesse")
    print("Aktualisieren | refresh=true")
    # Platten-Abschnitt im selben Eintrag: zwei SwiftBar-Einträge blendet macOS bei Platzmangel aus.
    disk_script = os.path.join(os.path.dirname(SELF), "memwatch-disk.30m.py")
    if os.path.exists(disk_script):
        out = subprocess.run([sys.executable, disk_script], capture_output=True, text=True, timeout=10).stdout
        print("---")
        print("\n".join(out.splitlines()[2:]))  # Titelzeile und erstes "---" weglassen


if len(sys.argv) >= 2 and sys.argv[1] == "kill" and len(sys.argv) == 3 and sys.argv[2].isdigit():
    kill_tree(int(sys.argv[2]))
elif len(sys.argv) == 2 and sys.argv[1] == "killall":
    for o in find_orphans(processes()):
        kill_tree(o[0])
else:
    main()
