## Summary

<!-- What changed and why. Link the task-contract issue (e.g. G0-A1). -->

## Authority

- Issue: <!-- e.g. #12 -->
- Spec section: <!-- e.g. docs/gates/G0.md#G0-A -->

## Deviations from the frozen specification

<!-- List any deviation from docs/gates/G0.md explicitly. "None" is a valid
     answer. Silent deviation is not allowed (AGENTS.md). -->

## Checklist

- [ ] `python -m pytest` passes (offline suite)
- [ ] `ruff check .` passes
- [ ] `python tools/check_hygiene.py` passes
- [ ] No provider payloads or secrets committed; `data/` untouched
- [ ] Evidence changes are metadata-only (manifests/hashes/counts/logs)
- [ ] CI is offline — no provider network access introduced
- [ ] Docs updated where behavior or commands changed
