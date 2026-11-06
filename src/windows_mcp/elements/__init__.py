"""Element identity, actionability, semantic actions and safety guards.

Shared by the label-based input tools (Click, Type, Scroll, Move, MultiSelect,
MultiEdit), ElementAction, Find and WaitFor:

* ``identity``      — live element handle captured per Snapshot label; STALE_REF checks
* ``actionability`` — pre-dispatch checks (visible, stable, enabled, uncovered) + auto-wait
* ``semantic``      — UI Automation pattern actions with read-back verification
* ``report``        — delivery dispositions and structured ``ElementError``
* ``resolve``       — label → verified target, the single path the tools use
* ``safety``        — protected processes, blocked shortcuts, UIPI integrity verdict
* ``waits``         — cheap live predicates for WaitFor
"""
