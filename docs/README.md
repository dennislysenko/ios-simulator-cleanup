# iOS Simulator Cleanup Toolkit

This workspace inventories where CoreSimulator stores data on this Mac and ships a helper CLI that surfaces the heaviest simulators plus safe cleanup actions.

## Storage map (quick recap)

See `docs/cleanup_plan.md` for the deep dive. Highlights:

- `~/Library/Developer/CoreSimulator/Devices/<UDID>` — per-simulator homes (the biggest data hogs).
- `~/Library/Logs/CoreSimulator/` — simulator log archives.
- `/Library/Developer/CoreSimulator/Caches/dyld` — dynamic loader caches (~7 GB, safe to rebuild).
- `/Library/Developer/CoreSimulator/Cryptex/Images/bundle` — runtime cryptex bundles (~8 GB for iOS 18).
- `/Library/Developer/CoreSimulator/Volumes` — mounted runtime volumes (~32 GB total here).

## Interactive Python TUI

```
python3 sim_cleanup.py
```

The Python TUI launches instantly with a list of simulators (UDID, name, runtime) while size calculations run in the background. Cached measurements and detail reports are reused between runs (and revalidated in the background), so previously scanned devices show their sizes immediately. Each row updates live (e.g. `Sizing… (4.2 GB)`) until the measurement completes. Use the keyboard to dig deeper and take action:

- `↑/↓` scroll the simulator list (Page Up/Down jump by a page).
- `Enter` loads a detailed report (top folders, largest app containers).
- `b` boots the selected simulator via `simctl`.
- `o` opens its data folder in Finder.
- `c` cleans caches/tmp (optional confirmation for destructive runs).
- `d` deletes the simulator (with force option for Booted states).
- `s` toggles between alphabetical order and "largest size first".
- `r` refreshes the device inventory and re-runs size probes.

Launch with `SIM_CLEANUP_DEBUG=1` to enable verbose logging of background work. Global totals now include Interface Builder simulator clones under `~/Library/Developer/Xcode/UserData/IB Support/Simulator Devices` so that hidden space is easy to spot.

---

## `sim_cleanup.py` CLI (automation-friendly)

```
python3 sim_cleanup.py --help
```

### Scan simulators

```
python3 sim_cleanup.py scan --top 10 --min-size-mb 512
```

Lists the largest simulator folders with runtime, state, last booted time, and a separate table of shared/global assets (dyld cache, runtime volumes, cryptex bundles, logs). Add `--json` for automation.

### Drill into a simulator

```
python3 sim_cleanup.py detail <UDID> --top 10
```

Shows a breakdown of heavy folders under `data/` and the largest app sandboxes (`Containers/Data/Application/*`), including bundle identifiers pulled from container metadata.

### Clean volatile data

```
python3 sim_cleanup.py clean <UDID> --targets device-caches app-caches tmp downloads logs --execute --yes
```

Targets:

- `device-caches` — clears `data/Library/Caches`.
- `app-caches` — clears `Library/Caches` inside each app container.
- `tmp`, `downloads`, `media`, `shared`, `bundles`, `logs`.

All destructive commands run in dry-run mode unless you add `--execute`. Use `--yes` to skip the confirmation prompt.

### Delete an entire simulator safely

```
python3 sim_cleanup.py delete-device <UDID> --execute --yes
```

Refuses to delete Booted simulators unless `--force` is provided.

### Purge shared assets

```
python3 sim_cleanup.py purge-global --dyld --logs --execute --yes
python3 sim_cleanup.py purge-global --volume iOS_23A343 --cryptex SimRuntimeBundle-… --execute --yes
```

Removes dyld caches, global logs, specific runtime volumes, or cryptex bundles. Requires explicit `--execute`.

### Notes & limitations

- `du` needs to walk runtime volumes; on macOS this may print warnings for protected folders. The script reports the permission error and treats the size as `0 B`.
- CoreSimulatorService isn’t available inside this sandbox, so the script does not depend on `simctl`.
- Always keep at least one healthy simulator for each runtime you actively use.
- If a simulator is stuck and Xcode says it’s “Booting…”, choose “Delete simulator” in the shell UI and opt into the force delete prompt—the script will attempt `xcrun simctl shutdown` before removing files.
