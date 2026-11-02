"""Parallel evaluation of many design points with progress reporting and cancellation."""

from __future__ import annotations

import os
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..core.design import EvaluationResult, evaluate


class Cancelled(Exception):
    pass


@dataclass
class Point:
    """One evaluation request: parameter overrides plus operating conditions."""

    params: dict[str, float] = field(default_factory=dict)
    corner: str | None = None
    temperature: float = 27.0
    vdd_scale: float = 1.0
    label: dict = field(default_factory=dict)   # what makes this point special (sweep values, PVT)

    def to_dict(self) -> dict:
        return {"params": self.params, "corner": self.corner, "temperature": self.temperature,
                "vdd_scale": self.vdd_scale, "label": self.label}


def default_workers() -> int:
    return int(os.environ.get("ANALOGFORGE_WORKERS", "0")) or max(1, (os.cpu_count() or 2))


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any], workers: int | None = None,
                 progress: Callable[[int, int, int, Any], None] | None = None,
                 cancel: threading.Event | Callable[[], bool] | None = None) -> list[Any]:
    """Apply ``fn`` to ``items`` on a thread pool, preserving order.

    ``progress(done, total, index, result)`` is called after each item;
    ``cancel`` (event or callable) stops scheduling new items and raises
    :class:`Cancelled` once running items finish.
    """
    items = list(items)
    total = len(items)
    results: list[Any] = [None] * total
    if not total:
        return results
    is_cancelled = (cancel.is_set if isinstance(cancel, threading.Event) else cancel) or (lambda: False)
    workers = max(1, min(workers or default_workers(), total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        it = iter(enumerate(items))
        for _ in range(workers):
            try:
                i, item = next(it)
            except StopIteration:
                break
            pending[pool.submit(fn, item)] = i
        while pending:
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in finished:
                i = pending.pop(fut)
                results[i] = fut.result()
                done += 1
                if progress:
                    progress(done, total, i, results[i])
                if not is_cancelled():
                    try:
                        j, item = next(it)
                        pending[pool.submit(fn, item)] = j
                    except StopIteration:
                        pass
        if is_cancelled() and done < total:
            raise Cancelled(f"cancelled after {done}/{total} points")
    return results


def evaluate_points(design, points: list[Point], backend="ngspice", pdk="sky130", options: dict | None = None,
                    workers: int | None = None, progress=None, cancel=None,
                    on_result: Callable[[int, Point, EvaluationResult], None] | None = None) -> list[EvaluationResult]:
    """Evaluate many points in parallel (each evaluation may itself run several testbenches)."""
    lock = threading.Lock()

    def one(arg):
        idx, pt = arg
        res = evaluate(design, pt.params, backend=backend, pdk=pdk, corner=pt.corner,
                       temperature=pt.temperature, vdd_scale=pt.vdd_scale, options=options)
        if on_result:
            with lock:
                on_result(idx, pt, res)
        return res

    return parallel_map(one, list(enumerate(points)), workers, progress, cancel)
