"""Multi-layer spin detection.

Five layers run in parallel; any can fire. See docs/LONG_HORIZON_AGENT.md §26.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Protocol

from horizonx.core.types import Session, SpinDetectionConfig, SpinReport, Step, StepType


class SpinLayer(Protocol):
    name: str

    async def check(self, session: Session, store: Any) -> SpinReport: ...


def _hash_step(step: Step) -> str:
    """Canonical hash for a tool call (tool_name + sorted args)."""
    if step.type != StepType.TOOL_CALL or step.tool_name is None:
        return ""
    norm = json.dumps(step.content, sort_keys=True, default=str)
    return hashlib.sha256(f"{step.tool_name}::{norm}".encode()).hexdigest()[:16]


class ExactLoopLayer:
    """Hard-abort when the identical tool call appears >= hard_threshold times.

    A soft_threshold (< hard_threshold) triggers a warn-and-inject rather than
    an abort, giving the model a chance to self-correct before we pull the plug.
    """

    name = "exact_loop"

    def __init__(
        self,
        hard_threshold: int = 3,
        window: int = 20,
        soft_threshold: int | None = None,
        *,
        threshold: int | None = None,  # backwards-compat alias for hard_threshold
    ):
        self.hard_threshold = threshold if threshold is not None else hard_threshold
        self.soft_threshold = soft_threshold if soft_threshold is not None else max(1, self.hard_threshold - 1)
        self.window = window

    async def check(self, session: Session, store: Any) -> SpinReport:
        steps = await store.recent_steps(session.id, self.window)
        hashes = [h for h in (_hash_step(s) for s in steps) if h]
        counts = Counter(hashes)
        for h, c in counts.most_common():
            if c >= self.hard_threshold:
                return SpinReport(
                    detected=True,
                    layer=self.name,
                    detail={"hash": h, "count": c, "window": self.window, "tier": "hard"},
                    action="terminate_session_and_retry",
                )
            if c >= self.soft_threshold:
                return SpinReport(
                    detected=True,
                    layer=self.name,
                    detail={"hash": h, "count": c, "window": self.window, "tier": "soft"},
                    action="warn_and_inject_diagnostic",
                )
        return SpinReport(detected=False)


class BucketedHashLayer:
    """Dual-threshold loop detector using 4-char hash buckets.

    Tolerates minor prompt variations that fool exact-string matching.
    The bucket groups semantically similar tool calls so repetition is caught
    even when arguments differ slightly (e.g. retrying the same path with a
    trivially different flag).

    soft_threshold → warn_and_inject_diagnostic (agent self-corrects)
    hard_threshold → terminate_session_and_retry (give up this session)
    """

    name = "bucketed_hash"

    def __init__(self, soft_threshold: int = 3, hard_threshold: int = 5, window: int = 30):
        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        self.window = window

    def _bucket(self, step: Step) -> str:
        if step.type != StepType.TOOL_CALL or step.tool_name is None:
            return ""
        norm = f"{step.tool_name}::{step.content.get('command', step.content.get('input', ''))}"
        return hashlib.sha256(norm.encode()).hexdigest()[:4]

    async def check(self, session: Session, store: Any) -> SpinReport:
        steps = await store.recent_steps(session.id, self.window)
        buckets = [b for b in (self._bucket(s) for s in steps) if b]
        if not buckets:
            return SpinReport(detected=False)
        counts = Counter(buckets)
        top_bucket, top_count = counts.most_common(1)[0]
        if top_count >= self.hard_threshold:
            return SpinReport(
                detected=True,
                layer=self.name,
                detail={"bucket": top_bucket, "count": top_count, "tier": "hard"},
                action="terminate_session_and_retry",
            )
        if top_count >= self.soft_threshold:
            return SpinReport(
                detected=True,
                layer=self.name,
                detail={"bucket": top_bucket, "count": top_count, "tier": "soft"},
                action="warn_and_inject_diagnostic",
            )
        return SpinReport(detected=False)


class EditRevertLayer:
    name = "edit_revert"

    async def check(self, session: Session, store: Any) -> SpinReport:
        steps = await store.recent_steps(session.id, 50)
        # Look at file_edit tool calls and detect A→B→A→B pattern
        edits: dict[str, list[str]] = {}
        for s in steps:
            if s.type != StepType.TOOL_CALL or s.tool_name not in ("Edit", "Write", "edit"):
                continue
            path = s.content.get("file_path") or s.content.get("path")
            if not path:
                continue
            edits.setdefault(path, []).append(_hash_step(s))
        for path, hist in edits.items():
            if len(hist) >= 4 and hist[-1] == hist[-3] and hist[-2] == hist[-4] and hist[-1] != hist[-2]:
                return SpinReport(
                    detected=True,
                    layer=self.name,
                    detail={"path": path, "history": hist[-4:]},
                    action="terminate_and_hitl",
                )
        return SpinReport(detected=False)


class ScorePlateauLayer:
    name = "score_plateau"

    def __init__(self, window: int = 3, delta: float = 0.02):
        self.window = window
        self.delta = delta

    async def check(self, session: Session, store: Any) -> SpinReport:
        scores = await store.recent_validator_scores(session.run_id, self.window)
        if len(scores) < self.window:
            return SpinReport(detected=False)
        avg = sum(scores) / len(scores)
        spread = max(scores) - min(scores)
        if spread < self.delta:
            return SpinReport(
                detected=True,
                layer=self.name,
                detail={"scores": scores, "spread": spread, "avg": avg},
                action="switch_strategy",
            )
        return SpinReport(detected=False)


class ToolThrashingLayer:
    name = "tool_thrashing"

    async def check(self, session: Session, store: Any) -> SpinReport:
        steps = await store.recent_steps(session.id, 30)
        tools = [s.tool_name for s in steps if s.type == StepType.TOOL_CALL and s.tool_name]
        if len(tools) < 10:
            return SpinReport(detected=False)
        # If same tool >= 70% of last 20 calls, suspicious
        c = Counter(tools[-20:])
        top, n = c.most_common(1)[0]
        if n / 20 >= 0.7 and top in {"Bash", "bash", "shell"}:
            return SpinReport(
                detected=True,
                layer=self.name,
                detail={"tool": top, "count": n, "window": 20},
                action="warn_and_inject_diagnostic",
            )
        return SpinReport(detected=False)


SEMANTIC_SPIN_SYSTEM = """\
You are a spin detector for a long-horizon agent execution framework.
Analyze the agent's recent trajectory and determine if it is making real
progress or spinning (repeating similar actions without advancement).

Signs of spinning:
- Same errors repeated without different fix attempts
- Files edited back and forth (A→B→A→B)
- Same commands run with trivially different arguments
- Long stretches of "thinking" with no tool calls or file changes
- Tests toggling between pass and fail on the same assertion

Signs of progress:
- New files created that didn't exist before
- Test count increasing
- Different approaches tried after failures
- Build/lint errors decreasing
- New functionality added (not just reformatting)

Output ONLY a JSON object:
{
  "spinning": true/false,
  "confidence": 0.0-1.0,
  "reason": "<1-2 sentence explanation>"
}
"""


class SemanticProgressLayer:
    """LLM-as-judge: 'is the agent making progress on the stated goal?'"""

    name = "semantic_progress"

    def __init__(self, model: str = "claude-haiku-4-5", every_n: int = 20):
        self.model = model
        self.every_n = every_n

    async def check(self, session: Session, store: Any) -> SpinReport:
        if session.steps_count == 0 or session.steps_count % self.every_n != 0:
            return SpinReport(detected=False)

        steps = await store.recent_steps(session.id, 40)
        if len(steps) < 10:
            return SpinReport(detected=False)

        lines: list[str] = []
        for s in steps:
            if s.type in (StepType.USAGE, StepType.SESSION_ID, StepType.SYSTEM):
                continue
            label = s.tool_name or s.type.value
            content = json.dumps(s.content, sort_keys=True, default=str)[:200]
            lines.append(f"[{s.sequence}] {label}: {content}")
        trajectory = "\n".join(lines)

        try:
            from horizonx.core.llm_client import call_llm_json

            result = await call_llm_json(
                system=SEMANTIC_SPIN_SYSTEM,
                user_prompt=f"TRAJECTORY (last {len(steps)} steps):\n{trajectory}",
                model=self.model,
                max_tokens=256,
                cache_system=True,
            )
        except Exception:
            return SpinReport(detected=False)

        if result.get("spinning") and result.get("confidence", 0) >= 0.7:
            return SpinReport(
                detected=True,
                layer=self.name,
                detail={
                    "reason": result.get("reason", ""),
                    "confidence": result.get("confidence"),
                    "model": self.model,
                },
                action="terminate_and_re_decompose",
            )
        return SpinReport(detected=False)


class SpinDetector:
    def __init__(self, config: SpinDetectionConfig, store: Any):
        self.config = config
        self.store = store
        self.layers: list[SpinLayer] = [
            ExactLoopLayer(
                hard_threshold=config.exact_loop_threshold,
                window=config.exact_loop_window,
                soft_threshold=config.soft_exact_loop_threshold,
            ),
        ]
        if config.edit_revert_enabled:
            self.layers.append(EditRevertLayer())
        self.layers.append(ScorePlateauLayer(config.score_plateau_window, config.score_plateau_delta))
        self.layers.append(ToolThrashingLayer())
        if getattr(config, "bucketed_hash_enabled", True):
            self.layers.append(
                BucketedHashLayer(
                    soft_threshold=config.bucketed_hash_soft_threshold,
                    hard_threshold=config.bucketed_hash_hard_threshold,
                    window=config.bucketed_hash_window,
                )
            )
        if config.semantic_layer_enabled:
            self.layers.append(
                SemanticProgressLayer(config.semantic_model, config.semantic_check_every_n_steps)
            )

    async def check(self, session: Session) -> SpinReport:
        for layer in self.layers:
            report = await layer.check(session, self.store)
            if report.detected:
                return report
        return SpinReport(detected=False)
