# Simulator Cleanup TUI – High-Level Plan

- **Unify the tooling**: Retire the shell script and focus entirely on an enhanced Python TUI as the single entry point for simulator inspection and maintenance.
- **Fast startup**: On launch, synchronously gather lightweight metadata (UDID, name, runtime/version) for all simulator directories so the list renders immediately.
- **Asynchronous sizing**: Kick off concurrent background jobs that compute folder sizes and stream progress back to the UI row-by-row (e.g., `Sizing… (4.2 GB)` until complete).
- **Interactive navigation**: Keep the list view responsive with smooth up/down movement, real-time detail panel updates, and clear status badges.
- **Action shortcuts**: Bind hotkeys for core actions (Boot, Open in Finder, Delete with confirmation, plus future operations like Clean caches) and surface inline feedback for each action. 
