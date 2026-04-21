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

## Post-3D P3 nits

1. `_build_recovery_fallback_payload` forwarding regression — implemented in
   PR #3, shipped 2026-04-21.

- Closeout: `_mark_turn_recovery_unsafe` now forwards `command_preview` or
  `summary` as the synthetic fallback event text through
  `TurnRecoveryHandle.from_store`, preserving pre-3D fallback semantics for
  first-writer unsafe recovery paths.

2. `message_id=None` same-turn detection collision — implemented in PR #3,
   shipped 2026-04-21.

- Closeout: `TurnRecoveryHandle.begin()` now requires non-`None` stamped
  message IDs on both sides before carrying forward retry metadata, so no-id
  turns cannot collide on text.
