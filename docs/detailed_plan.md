# Simulator Cleanup TUI – Detailed Plan

## 1. Project Restructure
- Remove references to `sim_cleanup.sh` from docs and tooling; deprecate the script itself.
- Make `sim_cleanup.py` (or a renamed module) the primary entry point, ensuring `python3 sim_cleanup.py` starts the TUI.
- Organize code into modules (`ui/`, `core/`, `workers/`) if the file becomes unwieldy, keeping the CLI entry in a thin wrapper.

## 2. Core Data Model
- Define data classes for simulator metadata, size jobs, and action results.
- Store metadata (UDID, name, runtime, state, path) gathered synchronously at launch.
- Track size state per simulator (`pending`, `running`, `done`, `error`) with current measured bytes for progressive updates.

## 3. Startup Flow
- On launch, enumerate `Devices` directory, parse `device.plist`, and populate the in-memory list immediately.
- Render the list UI with placeholder size labels (`Sizing…`) before background jobs begin.
- Initialize a thread pool or `concurrent.futures` executor dedicated to size computations.

## 4. Asynchronous Sizing
- Submit each simulator path to the executor; jobs run the existing `du -sk` helper.
- Emit intermediate updates: periodically read partial `du` output or chunk subdirectories to provide progress (fallback to final size if incremental data unavailable).
- Cache completed size results in memory; optionally persist to a lightweight cache file for subsequent runs.

## 5. UI Enhancements
- Maintain a curses-based list where the highlighted row drives a detail pane (top folders, log sizes, etc.).
- Display status badge per row: `Ready`, `Sizing…`, `4.2 GB`, `Error`.
- Include a footer with available shortcuts and contextual messages.

## 6. Input & Actions
- Map hotkeys:
  - `↑/↓` navigate
  - `Enter` opens a contextual action menu
  - `b` boot via `xcrun simctl boot <udid>`
  - `o` open simulator directory in Finder (`open <path>`)
  - `d` delete simulator (two-step confirmation, support `--force` equivalent)
  - Future: `c` clean caches, `l` show logs, `r` reset runtime services
- Show confirmation prompts inside the TUI; log outcomes in a status bar.

## 7. Background Action Handling
- Run potentially long operations (delete, clean) in worker threads to keep the UI responsive.
- Stream progress back to the UI with a thread-safe event queue.
- After actions complete, refresh cached metadata and sizes if state changed (e.g., after deletion).

## 8. Error Handling & Logging
- Use the existing logging setup, gated by `SIM_CLEANUP_DEBUG`, to trace background execution without polluting the UI.
- Surface errors in-line (e.g., row badge `Error – see logs`) and keep the app running.

## 9. Testing & Validation
- Add unit tests for metadata discovery, size job management, and action orchestration.
- Provide a lightweight mock mode that simulates a few devices for automated testing of the UI logic.
- Document how to run in debug mode, how to launch actions, and how caching behaves.
