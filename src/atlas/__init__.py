"""Atlas target structure (tracker ST-01, Phase 0 of the refactor plan).

This is the destination package for the modular-monolith decomposition
described in `Docs/10-architecture/04-module-decomposition.md`. It exists
alongside the current flat `aida` package and moves no behavior by itself --
see `Docs/40-engineering/06-refactor-plan.md` for the phased extraction that
populates it.

- `atlas.platform` -- shared infrastructure with no domain knowledge.
- `atlas.modules.<name>` -- the 21 bounded-context modules, each generated
  from `scripts/generate_module.py` and following the anatomy in
  `Docs/10-architecture/04-module-decomposition.md` Sec.7.
"""
