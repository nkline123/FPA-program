# FPA

A Python library for resolving financial measures across time periods and scenarios.

Handles fiscal calendars, measure dependencies, scenario awareness, and memoization.
Data access is left to the caller — wire in any data source via resolver callables.

## Install

```bash
pip install git+https://github.com/you/fpa.git
```

## Docs

- [USAGE.md](USAGE.md) — full API reference and resolver patterns
- [OVERVIEW.md](OVERVIEW.md) — concepts and design
- [smoke_test.py](smoke_test.py) — working end-to-end example
