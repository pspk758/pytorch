"""This module sets the user facing command APIs, both CLI and programmatic."""
import dataclasses
import itertools as it
import textwrap
from typing import cast, Dict, Iterable, Mapping, List, Optional, Tuple, TYPE_CHECKING

from core.api import Mode
from core.types import Label
from core.unpack_groups import unpack
from definitions.ad_hoc import ADHOC_BENCHMARKS
from definitions.standard import BENCHMARKS
from execution.future import WorkOrder
from execution.runner import Callback, Runner
from frontend.display import render_ab, ResultType, ValueType
from worker.main import CostEstimate, WorkerTimerArgs, WorkerOutput

if TYPE_CHECKING:
    # See core.api for an explanation why this is necessary.
    from torch.utils.benchmark.utils.timer import Language
else:
    from torch.utils.benchmark import Language


def _make_sentry(source_cmd: Optional[str]) -> WorkOrder:
    """Known stable tasks which are injected to test measurement stability."""
    timer_args = WorkerTimerArgs(
        stmt="""
            auto x = torch::ones({4, 4});
            auto z = x - y;
        """,
        setup="auto y = torch::ones({4, 4});",
        num_threads=1,
        language=Language.CPP,
        cost=CostEstimate.LESS_THAN_10_US,
    )

    return WorkOrder(
        label=("Impl", "Sentry"),
        mode=Mode.EXPLICIT_CPP,
        timer_args=timer_args,
        source_cmd=source_cmd,
        timeout=180.0,
        retries=2,
    )


class TimeReplicateCallback(Callback):
    def __init__(
        self,
        work_items_by_source_cmd: Tuple[Tuple[WorkOrder, ...], ...]
    ) -> None:
        n_source_cmds = len(work_items_by_source_cmd[0])
        assert all(len(w) == n_source_cmds for w in work_items_by_source_cmd)

        self._outstanding = [n_source_cmds for _ in work_items_by_source_cmd]
        self._results: Dict[WorkOrder, WorkerOutput] = {}
        self._work_items_by_source_cmd = work_items_by_source_cmd
        self._indices: Dict[WorkOrder, Tuple[int, int]] = {
            wij: (i, j)
            for i, wi in enumerate(work_items_by_source_cmd)
            for j, wij in enumerate(wi)
        }

    def __call__(
        self,
        work_order: WorkOrder,
        output: WorkerOutput,
    ) -> Iterable[WorkOrder]:
        assert work_order not in self._results
        self._results[work_order] = output

        if work_order not in self._indices:
            # TODO
            return ()

        i, j = self._indices[work_order]
        self._outstanding[i] -= 1

        if self._outstanding[i]:
            return ()

        work_items = self._work_items_by_source_cmd[i]
        results: Tuple[WorkerOutput, ...] = tuple(self._results[w] for w in work_items)
        counts: Tuple[int, ...] = tuple(
            r.instructions.counts(denoise=True) for r in results)

        if not all(c == counts[0] for c in counts):
            new_work_items: List[WorkOrder] = []
            for w in work_items:
                new_timer_args = dataclasses.replace(
                    w.timer_args,
                    collect_instructions=False,
                )
                new_work_items.extend([
                    dataclasses.replace(w, timer_args=new_timer_args)
                    for _ in range(3)  # TODO: tune number and exact algorithm.
                ])

            return new_work_items


        return ()


def _collect(
    source_cmds: Tuple[Optional[str], ...] = (None,),
    ad_hoc: bool = False
) -> Tuple[ResultType, ...]:
    all_work_items: List[WorkOrder] = []
    work_items_by_source_cmd: List[Tuple[WorkOrder, ...]] = []

    # Set up normal benchmarks
    benchmarks = ADHOC_BENCHMARKS if ad_hoc else BENCHMARKS
    for label, mode, timer_args in unpack(benchmarks):
        orders: Tuple[WorkOrder, ...] = tuple(
            WorkOrder(
                label=label,
                mode=mode,
                timer_args=timer_args,
                source_cmd=source_cmd,
                timeout=180.0,
                retries=2,
            )
            for source_cmd in source_cmds
        )
        work_items_by_source_cmd.append(orders)
        all_work_items.extend(orders)

    # Set up sentry measurements for warnings.
    sentry_work_items: List[Tuple[WorkOrder, ...]] = [
        tuple(_make_sentry(source_cmd) for _ in range(3))
        for source_cmd in source_cmds
    ]
    all_work_items = list(it.chain(*sentry_work_items)) + all_work_items

    # Collect measurements.
    results = Runner(work_items=tuple(all_work_items)).run()

    # Warn if there is ANY variation in instruction counts. While Python has
    # some jitter, C++ should be truly detministic.
    for source_cmd, work_items in zip(source_cmds, sentry_work_items):
        sentry_results = [results[w].instructions.counts() for w in work_items]
        if len(set(sentry_results)) > 1:
            print(textwrap.dedent(f"""
                WARNING: measurements are unstable. (source cmd: `{source_cmd}`)
                    Three C++ sentries were run which should have been completely
                    deterministic, but instead resulted in the following counts:
                      {sentry_results}
            """))

    # Organize normal benchmark results.
    output: List[ResultType] = []
    for work_items in work_items_by_source_cmd:
        output_i: List[Tuple[Label, int, Mode, ValueType]] = []
        for w in work_items:
            r = results[w]
            output_i.append((
                w.label,
                w.timer_args.num_threads,
                w.mode,
                (r.instructions, (r.wall_time,))
            ))
        output.append(tuple(output_i))
    return tuple(output)


def ab_test(source_a: str, source_b: str, ad_hoc: bool = False) -> None:
    results = _collect(
        source_cmds=(source_a, source_b),
        ad_hoc=ad_hoc,
    )
    render_ab(results[0], results[1])

    import pdb
    pdb.set_trace()

    # import importlib
    # import frontend.display
    # importlib.reload(frontend.display); frontend.display.render_ab(results[0], results[1])