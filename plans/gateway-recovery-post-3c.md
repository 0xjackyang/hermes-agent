# Gateway recovery post-3C queue

Created: 2026-04-20

## Purpose

Track restart-recovery follow-on work that was explicitly deferred so it does
not disappear after Patch 3C closes the current data-model invariants.

## 3D candidates

1. Pre-interrupt tool classification

- Problem: `GatewayRunner.stop()` samples `get_activity_summary()` after
  `agent.interrupt()`, which can lose `current_tool` state and misclassify a
  risky turn as replay-safe.
- Fix direction: sample activity before `interrupt()`, or teach `interrupt()`
  to preserve a pre-interrupt snapshot.

2. Pending-message recovery gap after first response send

- Problem: queued follow-up replay currently writes `pending_recovery` after
  the first response is sent, leaving a crash window that can silently drop the
  pending follow-up turn.
- Fix direction: write the recovery envelope before the first response send, or
  centralize queued-turn lifecycle handling.

3. `TurnRecoveryHandle` lifecycle refactor

- Problem: `pending_recovery` is still written, updated, and cleared from
  multiple independent paths.
- Fix direction: introduce a single turn-scoped lifecycle helper so safe,
  unsafe, replayed, and cleared transitions happen in one place.
