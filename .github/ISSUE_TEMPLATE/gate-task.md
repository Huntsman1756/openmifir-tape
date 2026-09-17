---
name: Gate task contract
about: A bounded work item tied to a frozen gate (the only valid issue form for G0 work)
title: "G0-XX: "
labels: planned
---

<!--
Per AGENTS.md, every work item is a task contract with exactly this shape.
Issues MUST NOT restate or reinterpret the frozen specification — an issue
defines scope, not semantics. Read the Authority section before acting.
An issue is moved to `ready-for-agent` only when its Authority section
exists and is frozen.
-->

```text
ID: G0-<letter><number>
Gate: G0-<letter>
Objective: <one sentence>
Authority: docs/gates/G0.md#G0-<letter>
Allowed scope: <files/packages this issue may touch>
Done when: <gate criteria satisfied, verbatim from the spec>
Evidence: evidence/<issue-id>/
```

<!-- Optional context below. Do not re-draft gate semantics here. -->
