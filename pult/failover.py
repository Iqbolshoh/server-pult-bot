"""Automatic failover: when one engine's limit runs out, the job carries on.

The chain is an ordered list of (engine, model, effort) steps in config.json. A
job walks it forward, once, and only ever because a window ran out or a model was
overloaded -- never because the task itself failed. Cooldowns are keyed by engine
and shared by every queued job, so once Claude reports exhaustion nothing probes
that wall again until the stored reset time.
"""

import time

from .core import fmt_clock, log
from .config import CFG
from .i18n import t
from .engines import (ENGINES, ENGINE_ORDER, clamp_effort, cooldown_until, engine_label,
                      engine_path, engine_nearly_dry, model_label)

def enabled():
    return bool(CFG["fallback_enabled"])
def chain():
    """The configured chain, dropping steps that name an engine we do not have."""
    steps = []
    for raw in CFG["fallback_chain"] or []:
        engine = (raw or {}).get("engine")
        if engine not in ENGINES:
            continue
        steps.append({
            "engine": engine,
            "model": raw.get("model") or "",
            "effort": raw.get("effort") or "",
        })
    return steps
def save_chain(steps):
    CFG["fallback_chain"] = steps
def step_at(index):
    steps = chain()
    return steps[index] if 0 <= index < len(steps) else None
def step_ready(step):
    """(usable now?, reason key, reset timestamp) for one chain step."""
    engine = step["engine"]
    if not engine_path(engine):
        return False, "missing", 0
    until = cooldown_until(engine)
    if until:
        return False, "cooling", until
    return True, "", 0
def next_step(start=0, exclude_engines=()):
    """First usable step at or after `start`. (index, step) or (None, None)."""
    steps = chain()
    for index in range(max(0, start), len(steps)):
        step = steps[index]
        if step["engine"] in exclude_engines:
            continue
        ok, _reason, _until = step_ready(step)
        if ok:
            return index, step
    return None, None
def engines_ready():
    """Engines that are installed and not cooling down, in chain order."""
    seen, ready = set(), []
    for step in chain():
        engine = step["engine"]
        if engine in seen:
            continue
        seen.add(engine)
        if step_ready(step)[0]:
            ready.append(engine)
    for engine in ENGINE_ORDER:
        if engine not in seen and engine_path(engine) and not cooldown_until(engine):
            ready.append(engine)
    return ready
def should_step_aside(engine):
    """Pre-emptive hop: this engine is nearly dry and another one is ready.

    The job that is already running finishes where it is; the *next* one starts
    further down the chain, so the last of a window is not spent on a queue.
    """
    if not enabled() or not engine_nearly_dry(engine):
        return False
    return any(other != engine for other in engines_ready())
def step_effort(step, engine):
    return clamp_effort(engine, step.get("effort") or CFG["effort"], step.get("model"))
def describe_step(index, step, current=None):
    """One line of the /fallback screen."""
    ok, reason, until = step_ready(step)
    if ok:
        state = t("failover.state.ready")
    elif reason == "missing":
        state = t("failover.state.missing")
    else:
        state = t("failover.state.cooling", clock=fmt_clock(until))
    effort = step.get("effort") or CFG["effort"] or "—"
    mark = "▶️" if current == index else f"{index + 1}."
    return t("failover.step_line", mark=mark, engine=engine_label(step["engine"]),
             model=model_label(step["engine"], step["model"]), effort=effort, state=state)
def move_step(index, delta):
    """Reorder the chain by one place. Returns the new index, or None."""
    steps = chain()
    target = index + delta
    if not (0 <= index < len(steps) and 0 <= target < len(steps)):
        return None
    steps[index], steps[target] = steps[target], steps[index]
    save_chain(steps)
    return target
def hop_reason_text(reason, engine, resets_at):
    if reason == "busy":
        return t("failover.reason.busy", engine=engine_label(engine))
    if resets_at:
        return t("failover.reason.limit_at", engine=engine_label(engine),
                 clock=fmt_clock(resets_at))
    return t("failover.reason.limit", engine=engine_label(engine))
def handover_prompt(prompt, from_engine):
    """What the next engine is told when the chain crosses engines.

    Without this paragraph a failover destroys work: the new engine starts cold on
    a working tree the previous one already edited, and redoes or contradicts it.
    """
    return t("failover.handover", engine=engine_label(from_engine), task=prompt)
def note_hop(job_id, from_engine, to_engine, index, reason, resets_at):
    log(f"job #{job_id}: {from_engine} -> {to_engine} (step {index + 1}, {reason})")
    return int(time.time())
