#!/usr/bin/python3
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""Menüleiste: freier Plattenplatz, Git-Worktrees und große Caches (nur Anzeige, löscht nichts).

Das Plugin blockiert nie: Es druckt immer den Cache (CACHE) und startet bei veralteten Daten
einen losgelösten Hintergrundlauf (`--refresh`, niedrige Priorität, Lock-Datei gegen Überlappung).
"""
import json
import os
import shutil
import subprocess
import sys
import time

# --- Konfiguration -------------------------------------------------------------
ROOTS = ["~"]  # Verzeichnisse, deren direkte Unterordner mit `.git`-Eintrag als Repo gelten
WARN_FREE_GB = 20
CRIT_FREE_GB = 10
TTL_SECONDS = 1800
TOP_WORKTREES = 15
CACHE = os.path.expanduser("~/Library/Caches/memwatch-disk.json")
LOCK = os.path.expanduser("~/Library/Caches/memwatch-disk.lock")
LOCK_STALE_SECONDS = 3600
GIT_TIMEOUT = 120
DU_TIMEOUT = 900
# Bekannte große Caches (Pfad mit ~). Nur vorhandene werden gemessen; Pfade innerhalb eines
# anderen Eintrags werden übersprungen, damit nichts doppelt zählt.
CACHES = [
    ("npm", "~/.npm"),
    ("pnpm", "~/Library/pnpm"),
    ("pnpm-Store (alt)", "~/.pnpm-store"),
    ("Library/Caches", "~/Library/Caches"),
    (".cache", "~/.cache"),
    ("Xcode DerivedData", "~/Library/Developer/Xcode/DerivedData"),
    ("Xcode DeviceSupport", "~/Library/Developer/Xcode/iOS DeviceSupport"),
    ("CoreSimulator", "~/Library/Developer/CoreSimulator"),
    ("Colima/Docker", "~/.colima"),
]
SELF = os.path.realpath(__file__)
HOME = os.path.expanduser("~")


def run(*cmd, timeout=GIT_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def free_gb():
    return shutil.disk_usage("/").free / 1024 ** 3


def du_kb(*paths):
    """Physische Größe in KB (`du -sk`, folgt keinen Symlinks). Hardlinks zählt du pro Aufruf einmal,
    über mehrere Worktrees hinweg (pnpm) kann die Summe daher mehr sein als der echte Verbrauch."""
    paths = [p for p in paths if os.path.lexists(p)]
    if not paths:
        return 0
    _, out = run("du", "-skP", *paths, timeout=DU_TIMEOUT)
    total = 0
    for line in out.splitlines():
        head = line.split("\t", 1)[0]
        if head.isdigit():
            total += int(head)
    return total


def fmt(kb):
    gb = kb / 1024 / 1024
    return f"{gb:.1f} GB" if gb >= 1 else f"{kb / 1024:.0f} MB"


def fmt_age(secs):
    days = secs // 86400
    if days >= 1:
        return f"{days} d"
    return f"{secs // 3600} h"


def git(path, *args):
    return run("git", "-C", path, "--no-optional-locks", *args)


# --- Hintergrundlauf ----------------------------------------------------------
def discover_worktrees():
    seen, result = set(), []
    for root in ROOTS:
        root = os.path.expanduser(root)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            cand = os.path.join(root, name)
            if not os.path.lexists(os.path.join(cand, ".git")) or os.path.realpath(cand) in seen:
                continue
            code, out = git(cand, "worktree", "list", "--porcelain")
            if code != 0:
                continue
            cur = None
            first = True
            for line in out.splitlines() + [""]:
                if line.startswith("worktree "):
                    cur = {"path": line[9:], "branch": None, "main": first, "bare": False}
                    first = False
                elif cur and line.startswith("branch "):
                    cur["branch"] = line[7:].replace("refs/heads/", "")
                elif cur and line == "bare":
                    cur["bare"] = True
                elif cur and line == "":
                    real = os.path.realpath(cur["path"])
                    if real not in seen and not cur["bare"] and os.path.isdir(cur["path"]):
                        seen.add(real)
                        result.append(cur)
                    cur = None
            seen.add(os.path.realpath(cand))
    return result


def inspect(wt):
    path = wt["path"]
    nm = [os.path.join(path, "node_modules")]
    for sub in ("apps", "packages", "services"):
        base = os.path.join(path, sub)
        if os.path.isdir(base) and not os.path.islink(base):
            try:
                nm += [os.path.join(base, d, "node_modules") for d in sorted(os.listdir(base))]
            except OSError:
                pass
    wt["size_kb"] = du_kb(path)
    wt["nm_kb"] = du_kb(*nm)
    code, out = git(path, "log", "-1", "--format=%ct")
    wt["commit_ts"] = int(out.strip()) if code == 0 and out.strip().isdigit() else None
    code, out = git(path, "status", "--porcelain")
    wt["dirty"] = code != 0 or bool(out.strip())
    code, out = git(path, "log", "@{u}..")
    wt["upstream"] = code == 0
    wt["unpushed"] = code != 0 or bool(out.strip())
    wt["removable"] = not wt["main"] and not wt["dirty"] and not wt["unpushed"]
    return wt


def measure_caches():
    paths = [(n, os.path.expanduser(p)) for n, p in CACHES if os.path.exists(os.path.expanduser(p))]
    reals = [os.path.realpath(p) for _, p in paths]
    out = []
    for i, (name, p) in enumerate(paths):
        if any(i != j and reals[i].startswith(reals[j] + os.sep) for j in range(len(paths))):
            continue
        out.append({"name": name, "path": p, "size_kb": du_kb(p)})
    return sorted(out, key=lambda c: -c["size_kb"])


def refresh():
    """Lock holen, messen, Cache atomar schreiben."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK) < LOCK_STALE_SECONDS:
                return
            os.remove(LOCK)
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    started = time.time()
    try:
        worktrees = sorted((inspect(w) for w in discover_worktrees()), key=lambda w: -w["size_kb"])
        data = {
            "ts": time.time(),
            "duration": 0,
            "worktrees": worktrees,
            "caches": measure_caches(),
        }
        data["duration"] = round(time.time() - started, 1)
        tmp = CACHE + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def refresh_running():
    try:
        return time.time() - os.path.getmtime(LOCK) < LOCK_STALE_SECONDS
    except OSError:
        return False


def spawn_refresh():
    if refresh_running():
        return
    subprocess.Popen(
        ["/usr/bin/nice", "-n", "19", sys.executable, SELF, "--refresh"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- Anzeige ------------------------------------------------------------------
def load():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def short(path):
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


def main():
    free = free_gb()
    color = "red" if free < CRIT_FREE_GB else "orange" if free < WARN_FREE_GB else None
    data = load()
    age = time.time() - data["ts"] if data else None
    running = refresh_running()
    if (age is None or age > TTL_SECONDS) and not running:
        spawn_refresh()
        running = True

    print(f"💾 {free:.0f} GB" + (f" | color={color}" if color else ""))
    print("---")
    print(f"Frei: {free:.1f} GB (warn < {WARN_FREE_GB}, kritisch < {CRIT_FREE_GB})" + (f" | color={color}" if color else ""))
    if data is None:
        print("Wird berechnet… (erster Lauf, kann einige Minuten dauern)" if running else "Noch keine Daten")
    else:
        note = " · wird neu berechnet…" if running else ""
        print(f"Stand vor {fmt_age(int(age)) if age >= 3600 else str(int(age // 60)) + ' min'}"
              f" (Messung {data['duration']:.0f} s){note} | size=11")
        wts = data["worktrees"]
        total = sum(w["size_kb"] for w in wts)
        rem = [w for w in wts if w["removable"]]
        print("---")
        print(f"Git-Worktrees ({len(wts)}) · {fmt(total)}")
        print(f"--Sauber und gepusht (entfernbar): {len(rem)} · {fmt(sum(w['size_kb'] for w in rem))}")
        print("--Größen per du; Hardlinks (pnpm) können zwischen Worktrees mehrfach zählen | size=11")
        print("-----")
        for w in wts[:TOP_WORKTREES]:
            name = os.path.basename(w["path"])
            mark = "✓ " if w["removable"] else ""
            col = " color=green" if w["removable"] else ""
            print(f"{mark}{name}: {fmt(w['size_kb'])} · nm {fmt(w['nm_kb'])} | font=Menlo size=12{col}")
            age_txt = fmt_age(int(data["ts"] - w["commit_ts"])) if w["commit_ts"] else "?"
            print(f"--Pfad: {short(w['path'])} | font=Menlo size=11")
            print(f"--Branch: {w['branch'] or '(detached)'} · letzter Commit vor {age_txt} | font=Menlo size=11")
            print(f"--{'Hauptverzeichnis des Repos' if w['main'] else 'Linked Worktree'} | font=Menlo size=11")
            print(f"--Ungesicherte Änderungen: {'ja' if w['dirty'] else 'nein'} | font=Menlo size=11")
            up = "ja" if w["unpushed"] and w["upstream"] else "kein Upstream" if not w["upstream"] else "nein"
            print(f"--Nicht gepusht: {up} | font=Menlo size=11")
            print(f"--Entfernbar: {'ja (sauber und gepusht)' if w['removable'] else 'nein'} | font=Menlo size=11")
        if len(wts) > TOP_WORKTREES:
            rest = wts[TOP_WORKTREES:]
            print(f"… {len(rest)} weitere · {fmt(sum(w['size_kb'] for w in rest))}")
        print("---")
        print("Große Caches")
        for c in data["caches"]:
            print(f"--{c['name']}: {fmt(c['size_kb'])} · {short(c['path'])} | font=Menlo size=11")
    print("---")
    print(f"Jetzt neu berechnen | bash={SELF} param1=--refresh terminal=false refresh=true")


if len(sys.argv) == 2 and sys.argv[1] == "--refresh":
    refresh()
else:
    main()
