"""Auditable registry of repository execution surfaces.

The registry is deliberately declarative: importing it must not import an LLM
provider, inspect Git, create a directory, or otherwise start a run.  Managed
entrypoint tests can therefore use this module as the authoritative inventory
instead of relying only on brittle source-text searches.

``management`` describes the Phase 1.1B contract, not an assertion that every
module can be safely invoked at import time.  In particular, batch drivers own
an experiment-level managed attempt while delegating each simulation to the
managed ``experiments.run_seed`` leaf.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT = "official_managed_research_entrypoint"
TEST_OR_DIAGNOSTIC_ENTRYPOINT = "test_or_diagnostic_entrypoint"
LIBRARY_API = "library_api"
DEPRECATED_OR_UNSUPPORTED = "deprecated_or_unsupported"

DIRECT_MANAGED = "direct_managed"
DELEGATED_MANAGED_DRIVER = "delegated_managed_driver"
ANALYSIS_MANAGED = "analysis_managed"
NULL_CONTEXT = "null_context"
DEDICATED_AUDIT = "dedicated_audit"
LIBRARY_UNMANAGED = "library_unmanaged"
UNSUPPORTED = "unsupported"

PROVIDER_DIRECT = "direct"
PROVIDER_INDIRECT = "indirect_child_runs"
PROVIDER_NONE = "none"
PROVIDER_DIAGNOSTIC_DIRECT = "diagnostic_direct"


@dataclass(frozen=True)
class EntrypointSpec:
    """One executable surface or intentionally exposed low-level API."""

    entrypoint_id: str
    path: str
    command: str
    category: str
    management: str
    call_chain: tuple[str, ...]
    outputs: tuple[str, ...]
    provider_access: str
    formal_research_allowed: bool
    notes: str = ""

    @property
    def writes_outputs(self) -> bool:
        return bool(self.outputs)


def _spec(
    entrypoint_id: str,
    path: str,
    command: str,
    category: str,
    management: str,
    call_chain: Iterable[str],
    outputs: Iterable[str] = (),
    provider_access: str = PROVIDER_NONE,
    formal_research_allowed: bool = False,
    notes: str = "",
) -> EntrypointSpec:
    return EntrypointSpec(
        entrypoint_id=entrypoint_id,
        path=path,
        command=command,
        category=category,
        management=management,
        call_chain=tuple(call_chain),
        outputs=tuple(outputs),
        provider_access=provider_access,
        formal_research_allowed=formal_research_allowed,
        notes=notes,
    )


ENTRYPOINTS: tuple[EntrypointSpec, ...] = (
    # Simulation leaves.  These are the only official entrypoints that may
    # construct a simulation Provider directly.
    _spec(
        "nmsim.run.cli", "nmsim/run.py", "python3 -m nmsim.run",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DIRECT_MANAGED,
        ("bootstrap", "full config validation", "ManagedRunContext", "run_sim", "export"),
        ("managed run directory", "manifest/events", "canonical simulation outputs"),
        PROVIDER_DIRECT, True,
        "Primary research CLI; record and strict replay share the same managed boundary.",
    ),
    _spec(
        "nmsim.run.api", "nmsim/run.py", "nmsim.run.run(config, ...)",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DIRECT_MANAGED,
        ("validated Config", "ManagedRunContext", "run_sim", "export"),
        ("managed run directory", "manifest/events", "canonical simulation outputs"),
        PROVIDER_DIRECT, True,
        "Managed high-level Python API; unlike nmsim.sim.run_sim it has filesystem effects.",
    ),
    _spec(
        "experiments.run_seed", "experiments/run_seed.py",
        "python3 -m experiments.run_seed",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DIRECT_MANAGED,
        ("bootstrap", "full config validation", "ManagedRunContext", "run_sim or CSV reuse", "export"),
        ("managed run directory", "experiment_result.json", "legacy flat result projection"),
        PROVIDER_DIRECT, True,
        "Leaf used by all batch drivers; CSV reuse is provider-free but still managed.",
    ),
    _spec(
        "experiments.capture_traces", "experiments/capture_traces.py",
        "python3 -m experiments.capture_traces",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DIRECT_MANAGED,
        ("bootstrap", "full config validation", "ManagedRunContext", "run_sim", "private trace export"),
        ("managed run directory", "private reasoning trace"),
        PROVIDER_DIRECT, True,
        "Private rationale belongs only in a 0600 private artifact, never a public summary.",
    ),
    _spec(
        "experiments.model_qualification", "experiments/model_qualification.py",
        "python3 -m experiments.model_qualification",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DIRECT_MANAGED,
        (
            "bootstrap",
            "frozen fixture/rubric validation",
            "Phase 1.2A provider guard",
            "ManagedRunContext",
            "48 qualification cases or dry-run",
            "public/private export",
        ),
        (
            "managed qualification run directory",
            "public case results and aggregate diagnostics",
            "0600 private case records",
        ),
        PROVIDER_DIRECT, True,
        "Not a market simulation. Phase 1.2A permits only Mock/Fake and rejects external providers before construction.",
    ),

    # Experiment-level drivers.  Their run-count lifecycle is managed at the
    # driver level; every simulation remains a separate managed run_seed child.
    *(
        _spec(
            f"experiments.{module}", f"experiments/{module}.py",
            f"python3 -m experiments.{module}",
            OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, DELEGATED_MANAGED_DRIVER,
            ("driver bootstrap", "managed experiment attempt", "subprocess experiments.run_seed", "driver summary"),
            ("experiment summary", "failure-attempt log", "managed child runs"),
            PROVIDER_INDIRECT, True,
            "Provider access occurs only in managed run_seed children; driver honest-N unit is runs.",
        )
        for module in (
            "drive", "grid2x2", "sweep", "ablate", "lev2x2", "phase2b", "critsweep"
        )
    ),

    # Derived research artifacts.  These do not run a market or call a Provider,
    # but their inputs and outputs still need immutable provenance.
    _spec(
        "experiments.aggregate_grid", "experiments/aggregate_grid.py",
        "python3 -m experiments.aggregate_grid",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load per-run JSON", "aggregate", "export"),
        ("grid_summary.json", "envelope_2x2.png"), PROVIDER_NONE, True,
        "Management records the existing statistic; it does not validate its identification design.",
    ),
    _spec(
        "experiments.aggregate_seeds", "experiments/aggregate_seeds.py",
        "python3 -m experiments.aggregate_seeds",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load per-seed JSON", "aggregate", "export"),
        ("gain summary JSON", "gain envelope PNG"), PROVIDER_NONE, True,
        "Legacy gain analysis remains reproducible but must retain its known semantic caveats.",
    ),
    _spec(
        "experiments.aggregate_sweep", "experiments/aggregate_sweep.py",
        "python3 -m experiments.aggregate_sweep",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load paired sweep JSON", "aggregate", "export"),
        ("sweep_main.png",), PROVIDER_NONE, True,
        "Phase 1.1B must not silently change the historical CI calculation.",
    ),
    _spec(
        "experiments.calib_n", "experiments/calib_n.py",
        "python3 -m experiments.calib_n",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load calibration JSON", "compute planned N", "export"),
        ("calib_N.txt",), PROVIDER_NONE, True,
        "This output becomes an input to a later experiment and therefore requires provenance.",
    ),
    _spec(
        "experiments.lev_analyze", "experiments/lev_analyze.py",
        "python3 -m experiments.lev_analyze",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load leverage results", "analyze", "export"),
        ("leverage_2x2 PNG",), PROVIDER_NONE, True,
    ),
    _spec(
        "experiments.critsweep_analyze", "experiments/critsweep_analyze.py",
        "python3 -m experiments.critsweep_analyze",
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT, ANALYSIS_MANAGED,
        ("managed analysis attempt", "load critsweep and baseline results", "analyze", "export"),
        ("critsweep PNG",), PROVIDER_NONE, True,
    ),

    # Diagnostics and offline inspection.  They are deliberately not formal
    # provenance-complete research results.
    _spec(
        "experiments.repro_check", "experiments/repro_check.py",
        "python3 -m experiments.repro_check",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
        ("spawn PYTHONHASHSEED variants", "low-level run_sim", "compare stdout"),
        (), PROVIDER_NONE, False,
        "Deterministic Mock regression, not an experiment run.",
    ),
    _spec(
        "experiments.leverage_demo", "experiments/leverage_demo.py",
        "python3 -m experiments.leverage_demo",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
        ("build two Mock Configs", "low-level run_sim", "print comparison"),
        (), PROVIDER_NONE, False,
        "Intentional no-filesystem smoke using NullRunContext.",
    ),
    _spec(
        "nmsim.reparse_audit", "nmsim/reparse_audit.py",
        "python3 -m nmsim.reparse_audit",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, DEDICATED_AUDIT,
        ("read historical recording", "current parser", "immutable offline audit export"),
        ("reparse summary", "public field diff", "0600 private rationale diff"),
        PROVIDER_NONE, False,
        "Dedicated audit lifecycle; not strict replay and never continues a market.",
    ),
    _spec(
        "nmsim.prompts.preview", "nmsim/prompts.py", "python3 -m nmsim.prompts",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
        ("construct example prompt", "print"), (), PROVIDER_NONE, False,
    ),
    _spec(
        "experiments.bench_concurrency", "experiments/bench_concurrency.py",
        "python3 -m experiments.bench_concurrency",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
        ("construct direct AsyncOpenAI client", "endpoint benchmark", "print"),
        (), PROVIDER_DIAGNOSTIC_DIRECT, False,
        "Network diagnostic only; excluded from research runs and offline regression.",
    ),
    *(
        _spec(
            f"experiments.{module}", f"experiments/{module}.py",
            f"python3 -m experiments.{module}",
            TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
            ("load existing artifacts", "print diagnostic analysis"),
            (), PROVIDER_NONE, False,
            "Stdout-only historical inspection; not a provenance-complete result.",
        )
        for module in (
            "aggregate_mechanism", "analyze_traces", "flow_decomp", "ablation", "additive_test"
        )
    ),

    # The test suite is represented as one supported command.  Individual test
    # modules also have unittest.main guards, but they share this same policy.
    _spec(
        "tests.unittest_discovery", "tests/test_*.py",
        "python3 -m unittest discover -s tests -v",
        TEST_OR_DIAGNOSTIC_ENTRYPOINT, NULL_CONTEXT,
        ("unittest discovery", "test helpers or low-level APIs"),
        ("temporary test artifacts",), PROVIDER_NONE, False,
        "Tests must mock or use MockLLM; temporary outputs are not research artifacts.",
    ),

    # Explicit low-level library boundary.
    _spec(
        "nmsim.sim.run_sim", "nmsim/sim.py", "nmsim.sim.run_sim(config, llm, tracker, ...)",
        LIBRARY_API, LIBRARY_UNMANAGED,
        ("caller-supplied Config/LLM/tracker", "in-memory simulation", "SimResult"),
        (), PROVIDER_NONE, False,
        "Does not create directories, inspect Git, build a Provider, or claim managed provenance.",
    ),

    # Historical forks/wrappers that must not be legitimised by attaching a new
    # manifest to their divergent behavior.
    _spec(
        "narrative_market_sim", "narrative_market_sim.py", "python3 narrative_market_sim.py",
        DEPRECATED_OR_UNSUPPORTED, UNSUPPORTED,
        ("independent legacy Config/Agent/Mock/market loop", "hard-coded export"),
        ("/mnt/user-data/outputs/price_path.csv", "/mnt/user-data/outputs/price_path.png"),
        PROVIDER_NONE, False,
        "Diverged Phase-1 single-file implementation; not the package simulation.",
    ),
    _spec(
        "run_pipeline", "run_pipeline.sh", "bash run_pipeline.sh",
        DEPRECATED_OR_UNSUPPORTED, UNSUPPORTED,
        ("hard-coded calibration", "calib_n", "hard-coded real-provider sweep"),
        ("sweep_pipeline.log", "results_sweep artifacts"), PROVIDER_INDIRECT, False,
        "Historical shell pipeline; fixed output paths and no parent managed lifecycle.",
    ),
)


ENTRYPOINT_BY_ID = {spec.entrypoint_id: spec for spec in ENTRYPOINTS}


def by_category(category: str) -> tuple[EntrypointSpec, ...]:
    """Return registry entries in declaration order for ``category``."""

    return tuple(spec for spec in ENTRYPOINTS if spec.category == category)


def official_entrypoints() -> tuple[EntrypointSpec, ...]:
    """Return every entrypoint allowed to produce a formal research artifact."""

    return by_category(OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT)


def validate_registry() -> None:
    """Fail closed on duplicate or internally inconsistent declarations."""

    categories = {
        OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT,
        TEST_OR_DIAGNOSTIC_ENTRYPOINT,
        LIBRARY_API,
        DEPRECATED_OR_UNSUPPORTED,
    }
    management_modes = {
        DIRECT_MANAGED,
        DELEGATED_MANAGED_DRIVER,
        ANALYSIS_MANAGED,
        NULL_CONTEXT,
        DEDICATED_AUDIT,
        LIBRARY_UNMANAGED,
        UNSUPPORTED,
    }
    provider_modes = {
        PROVIDER_DIRECT,
        PROVIDER_INDIRECT,
        PROVIDER_NONE,
        PROVIDER_DIAGNOSTIC_DIRECT,
    }
    ids = [spec.entrypoint_id for spec in ENTRYPOINTS]
    if len(ids) != len(set(ids)):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        raise ValueError(f"duplicate entrypoint ids: {duplicates}")
    for spec in ENTRYPOINTS:
        if spec.category not in categories:
            raise ValueError(f"unknown category for {spec.entrypoint_id}: {spec.category}")
        if spec.management not in management_modes:
            raise ValueError(f"unknown management for {spec.entrypoint_id}: {spec.management}")
        if spec.provider_access not in provider_modes:
            raise ValueError(
                f"unknown provider policy for {spec.entrypoint_id}: {spec.provider_access}"
            )
        if spec.formal_research_allowed != (
            spec.category == OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT
        ):
            raise ValueError(
                f"formal-research/category mismatch for {spec.entrypoint_id}"
            )
        if spec.category == OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT and spec.management not in {
            DIRECT_MANAGED, DELEGATED_MANAGED_DRIVER, ANALYSIS_MANAGED
        }:
            raise ValueError(f"official entrypoint is not managed: {spec.entrypoint_id}")


validate_registry()


__all__ = [
    "ANALYSIS_MANAGED",
    "DEDICATED_AUDIT",
    "DELEGATED_MANAGED_DRIVER",
    "DEPRECATED_OR_UNSUPPORTED",
    "DIRECT_MANAGED",
    "ENTRYPOINTS",
    "ENTRYPOINT_BY_ID",
    "EntrypointSpec",
    "LIBRARY_API",
    "LIBRARY_UNMANAGED",
    "NULL_CONTEXT",
    "OFFICIAL_MANAGED_RESEARCH_ENTRYPOINT",
    "TEST_OR_DIAGNOSTIC_ENTRYPOINT",
    "UNSUPPORTED",
    "by_category",
    "official_entrypoints",
    "validate_registry",
]
