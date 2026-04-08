# iOS Simulator Storage Map & Cleanup Script Plan

## Storage locations discovered on this machine
- `~/Library/Developer/CoreSimulator/Devices` (per-simulator home folders)  
  - Largest directories right now include:
    - `07FFA23B-B19C-4F7B-8833-4CF72DB4DE0D` (≈10 GB, iPhone 16 / iOS 18.0)
    - `2E6F063B-FEC8-4DFA-B1AC-D178E31F57DD` (≈4.4 GB)
    - `7976B875-42E5-468B-B5F0-819514A7E307` (≈3.9 GB)
    - `1C849F37-FA9D-46C1-B9C5-ED9C12A5356A` (≈3.4 GB)
  - Typical heavy subfolders per device:
    - `data/Containers/Data` (app sandboxes, >4 GB in the 10 GB example)
    - `data/Containers/Bundle` (installed app bundles, ≈1.1 GB)
    - `data/Library` (system caches/logs, ≈1 GB)
    - `data/Media` plus `data/private` / `data/var` (system data, ≈1–1.6 GB each)
- `~/Library/Logs/CoreSimulator` (per-device logs, up to ≈2.7 MB per device; global logs `CoreSimulator.log` + `CoreSimulator.prev.log` ≈34 MB total)
- `/Library/Developer/CoreSimulator/Caches/dyld` (≈7.3 GB of dynamic loader caches, safe to rebuild)
- `/Library/Developer/CoreSimulator/Cryptex/Images` (≈7.8 GB runtime cryptex bundles)
- `/Library/Developer/CoreSimulator/Volumes` (≈32 GB of mounted runtime volumes: `iOS_22A3351`, `iOS_23A343`)
- `~/Library/Developer/Xcode/UserData/IB Support/Simulator Devices` (Interface Builder clones of simulators; reclaimed when old IB previews accumulate)
- Other smaller folders to keep in mind:
  - `~/Library/Developer/CoreSimulator/Temp` (currently empty except for placeholder)
  - `~/Library/Developer/CoreSimulator/Caches` (mostly symlinked to the dyld cache above)
  - `~/Library/Developer/CoreSimulator/Devices/device_set.plist` for metadata / default devices

## Cleanup script objectives
1. Provide visibility
   - Enumerate `Devices` directories, parse `device.plist` for friendly names, runtimes, last boot.
   - Compute total size per device and surface the top offenders.
   - Optionally display per-device breakdown (`Containers/Data`, `Containers/Bundle`, `Library`, `Media`, `tmp`, `Downloads`, etc.).
   - Report shared/global folders (dyld cache, runtime volumes, cryptex images, simulator logs) with sizes.
2. Assist with cleanup
   - `--delete-device <udid>` (with confirmation, refusal if simulator is Booted).
   - `--clean <udid> --targets caches,tmp,downloads,media,logs,bundles` to selectively purge high-churn folders.
   - `--purge-global dyld|logs|volumes=<name>|cryptex=<id>` to tidy shared assets once confirmed unused.
   - Dry-run mode by default; optional `--yes` to skip prompts.
   - Smart safeguards (warn if last booted recently, keep logs unless requested).
3. Ergonomics
   - Sortable tabular output (`--json` for scripting).
   - Threshold filters (`--min-size`, `--limit`).
   - Exit codes for automation (0 success, >0 on failure).
   - Works without `simctl` (CoreSimulatorService is not available in this sandbox).

## Implementation outline
1. Discover devices:
   - Walk `~/Library/Developer/CoreSimulator/Devices`.
   - For each directory containing `device.plist`, load metadata via Python `plistlib`.
   - Compute directory sizes via `du -sk` (fast, avoids Python traversal).
2. Per-device drill-down:
   - Predefined subpaths to measure (only if they exist).
   - Gather associated log folder sizes (`~/Library/Logs/CoreSimulator/<UDID>`).
3. Global assets:
   - Measure handful of well-known paths (dyld cache, cryptex bundle, runtime volumes).
   - For volumes/cryptex, expose identifiers (folder names) for deletion commands.
4. CLI surface (argparse):
   - Subcommands: `scan`, `detail`, `clean`, `delete-device`, `purge-global`.
   - Common flags: `--dry-run/--no-dry-run`, `--yes`, `--min-size`, `--top`.
   - Pretty-print table via `tabulate`-style helper (no external deps; format using string widths).
5. Cleanup actions:
   - Deletion uses `shutil.rmtree` or `os.remove` with dry-run gate.
   - When cleaning caches, remove contents rather than directories to avoid breaking symlinks.
   - Record operations in summary output.
6. Safety nets:
   - Refuse to operate on paths outside expected simulator directories.
   - Validate UDIDs against discovered set.
   - Confirm runtime volume/cryptex deletion is requested explicitly (`--purge-runtime iOS_22A3351`, etc.).

Deliverables:
- Enhanced `sim_cleanup.py` interactive TUI for day-to-day cleanup.
- Command-line subcommands in the same Python tool for automation / scripting.
- Updated README with usage examples and warnings.
