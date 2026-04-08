# sim_cleanup

**Reclaim tens of gigabytes from your iOS Simulators — without losing your mind.**

If you're an iOS developer, you already know the feeling: your Mac is out of disk space, and somehow `~/Library/Developer/CoreSimulator` has quietly eaten 80 GB. Old app bundles. Stale caches. Runtime volumes from Xcode versions you don't even have installed anymore. Simulators you booted once, two years ago.

`sim_cleanup` is a single-file Python tool with a focused TUI that shows you exactly where the bloat is and lets you reclaim it safely and interactively.

## Why you'll like it

- **You can see what you're deleting.** Every simulator in one view, with size color cues that make the worst offenders obvious. Drill in to see exactly which apps, caches, and media are eating your disk.
- **Batch operations.** Multi-select a dozen dead simulators and sweep them out in one confirmation.
- **Fast post-upgrade cleanup.** After an Xcode upgrade, you can mark every simulator that isn't on the latest iOS runtime and delete them with a few keystrokes: `m`, `a`, `d`, `y`, `y`.

## Install

```bash
curl -O https://raw.githubusercontent.com/dennislysenko/ios-simulator-cleanup/main/sim_cleanup.py
chmod +x sim_cleanup.py
```

## Launch the TUI

```bash
./sim_cleanup.py
```

That drops you into the main view: every simulator on your machine, with sizes color-coded so the worst offenders jump out. Hit `s` to toggle between name and size sorting.

### Keys

| Key | Action |
|---|---|
| `↑`/`↓` or `j`/`k` | Move |
| `g` / `G` | Jump to top / bottom |
| `PgUp` / `PgDn` | Page up/down |
| `Enter` | Drill into a simulator's breakdown |
| `d` | Delete the selected simulator |
| `s` | Toggle sort (name / size) |
| `r` | Refresh |
| `m` | Enter multi-select mode |
| `c` | Clean the selected simulator |
| `b` | Boot the selected simulator |
| `o` | Open the selected simulator in Finder |
| `q` / `Esc` | Back / quit |

### Multi-select mode

Hit `m`, then `space` to toggle simulators, `a` to mark outdated iOS runtimes, and `Enter` (or `d`) to delete the marked set in one go. `X` (or `c`) clears the current marks. Batch delete shows how many simulators are targeted, estimates reclaimed space, and waits for confirmation before touching anything.

### Drill-down view

Open a simulator with `Enter` and you'll see a full breakdown: installed apps, caches, downloads, media, logs, tmp, shared containers, and more, each with its size. Use `c` from the main view to clean individual categories without nuking the whole device.

## What it can clean

Per-simulator, with fine-grained targets:

| Target | What it removes |
|---|---|
| `app-caches` | Installed app `Caches/` directories |
| `bundles` | Installed `.app` bundles |
| `device-caches` | Device-wide caches |
| `downloads` | Safari and app downloads |
| `logs` | Per-device log archives |
| `media` | Photos, videos, screenshots |
| `shared` | Shared container caches |
| `tmp` | `tmp/` directories |

Plus **global CoreSimulator assets** that aren't tied to any one device: dyld cache, global logs, orphaned runtime volumes, and cryptex bundles.

## CLI (for scripting & automation)

The same operations are available as subcommands, so you can wire cleanup into CI, a cron job, or a `make clean` target. Everything destructive is dry-run by default — pass `--execute` to actually apply.

```bash
./sim_cleanup.py scan                                     # list simulators, biggest first
./sim_cleanup.py scan --top 10 --json                     # machine-readable
./sim_cleanup.py detail <UDID>                            # breakdown for one device
./sim_cleanup.py clean <UDID> --targets app-caches tmp logs --execute --yes
./sim_cleanup.py delete-device <UDID> --execute --yes
./sim_cleanup.py purge-global --dyld --logs --execute --yes
./sim_cleanup.py purge-global --volume iOS_23A343 --execute --yes
```

Run `./sim_cleanup.py <command> --help` for the full flag list on any subcommand.

## Requirements

- macOS
- Python 3.8+
- iOS Simulators that have gotten out of hand

## License

MIT
