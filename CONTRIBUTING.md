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
pip install -r requirements.txt
python synology_drive_monitor.py
```

## Pull Request Process

1. Fork the repo and create your branch from `main`.
2. Test on a Windows machine with an active Synology Drive Client session.
3. Ensure no memory leaks or unhandled thread exceptions.
4. Submit your pull request with clear descriptions of your improvements.
