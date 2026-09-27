"""The 21 bounded-context modules (`Docs/10-architecture/04-module-decomposition.md`
Sec.4, the module register).

Each `atlas.modules.<name>` package uses the anatomy's file names for the
files it has, and holds no file with nothing in it: R11-X4 removed the
docstring-only stubs once the S6 relocation was decided against. A new module
still starts from `scripts/generate_module.py`. This directory holds only
module packages -- no code of its own.
"""
