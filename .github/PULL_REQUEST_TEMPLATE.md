## What and why

<!-- What does this change, and what problem does it solve? -->

## How it was verified

<!-- Paste the actual output, not "tests pass". -->

```bash
python3 -m unittest discover
```

- [ ] `python3 -m py_compile common.py monitor.py vps_notifier.py configure_telegram.py tests/*.py`
- [ ] `python3 -m unittest discover -v`
- [ ] `for f in scripts/*.sh; do bash -n "$f"; done`
- [ ] `python3 monitor.py --validate-config --config config.example.json`
- [ ] Exercised against a real CodexBar install / VPS (say which, or say neither)

## Invariants

- [ ] Standard library only; `requirements.txt` still declares no packages
- [ ] No AI, LLM, or paid API call was added
- [ ] No reset interval is hard-coded; projection still uses the provider's `windowMinutes`
- [ ] No secret appears in code, config, logs, or error messages
- [ ] No inbound VPS port, no `sudo`, no writes outside `vps_remote_dir`
- [ ] No test performs network access or sends a real Telegram message
- [ ] Anything passed to `ssh` is quoted with `shell_quote()` for the remote shell
- [ ] `common.py` changed → the VPS needs redeploying (noted in the description)

## Tests

- [ ] Behavior change to projection, formatting, deduplication, or config validation is covered by a new test
- [ ] Or: this change has no runtime surface (docs, comments)
