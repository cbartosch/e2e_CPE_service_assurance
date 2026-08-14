"""Fixture-backed simulation of the ten external systems.

Import the factory from `lpr_cpe.integrations` or from `lpr_cpe.simulation.loader`:

    from lpr_cpe.integrations import build_simulated_adapters

This package `__init__` deliberately re-exports **nothing**. `simulation.loader` imports the ten
simulators, which live under `lpr_cpe.integrations`, and importing a submodule initialises its
parent package -- so a re-export here would close the loop `simulation -> integrations ->
simulation` and whichever of the two a process imported second would see a half-initialised module.
Keeping this file empty makes the import graph acyclic by construction rather than by luck;
`integrations/__init__.py` carries the matching note on its side.
"""
