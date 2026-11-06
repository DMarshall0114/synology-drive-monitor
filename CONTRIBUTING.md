# Contributing to Synology Drive Monitor

Thank you for considering contributing to Synology Drive Monitor!

## Principles

1. **Safety First**: Any database access MUST be strictly read-only (`mode=ro`, `PRAGMA query_only=ON`). Never introduce any write, delete, or schema-modifying queries.
2. **True Observation**: Never fabricate ETA or progress. If a metric cannot be stably estimated from local logs or databases, gracefully downgrade the state.
3. **Fluent Aesthetics**: Maintain consistency with Windows 11 Fluent Light design principles.

## Development Setup

```bash
git clone https://github.com/DMarshall0114/synology-drive-monitor.git
cd synology-drive-monitor
pip install -r requirements-dev.txt
python synology_drive_monitor.py
```

## Code Layout

- `drive_core.py`: read-only collection and metrics. Keep it free of tkinter so it stays unit-testable.
- `synology_drive_monitor.py`: UI and CLI only. Never call Tk from the worker thread; post results to the queue.

## Tests

```bash
python -m pytest
ruff check . && ruff format --check .
```

Add a test under `tests/` for every parsing or classification change, using synthetic `daemon.log` / `history.sqlite` fixtures. Never commit real Drive logs or databases (they contain your file paths).

## Pull Request Process

1. Fork the repo and create your branch from `main`.
2. Make sure `pytest` and `ruff` pass, then test on a Windows machine with an active Synology Drive Client session.
3. Ensure no memory leaks or unhandled thread exceptions.
4. Submit your pull request with clear descriptions of your improvements.
