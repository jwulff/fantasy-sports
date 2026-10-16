"""Expert *sources* — upstreams that carry no leagues (ADR-0011).

A :mod:`~fantasy_sports.providers` adapter answers "what is happening in my
league"; a source answers "what do the experts think about these players". It
has no leagues, teams or rosters, so it never pretends to satisfy
``providers/base.py::Provider``. It shares the credential chain (``auth/``) and
the HTTP cache (``cache/``), and like every layer below ``reports/`` it never
imports typer, click, rich, or a model.

One source ships: :mod:`fantasy_sports.sources.fantasypros`.
"""
