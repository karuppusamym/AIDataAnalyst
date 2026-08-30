"""Tests for `scripts/generate_module.py` (tracker ST-01: target structure +
module template). These exercise the generator itself against a temp
directory -- they do not touch `src/atlas/` and have no effect on the rest of
the application, matching the Phase-0 refactor-plan rule that every phase is
independently shippable and revertible.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR_PATH = _REPO_ROOT / "scripts" / "generate_module.py"

_spec = importlib.util.spec_from_file_location("generate_module", _GENERATOR_PATH)
assert _spec is not None and _spec.loader is not None
_generate_module_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_generate_module_script)
generate_module = _generate_module_script.generate_module

_EXPECTED_ANATOMY_FILES = {
    "__init__.py",
    "api.py",
    "contracts.py",
    "router.py",
    "service.py",
    "models.py",
    "schemas.py",
    "repository.py",
    "events.py",
    "workers/__init__.py",
    "migrations/README.md",
    "tests/__init__.py",
    "tests/test_module_scaffold.py",
}


def test_generated_module_has_the_full_anatomy_from_the_decomposition_doc(tmp_path: Path) -> None:
    written = generate_module("sample_module", dest_root=tmp_path)

    relative_paths = {
        path.relative_to(tmp_path / "sample_module").as_posix() for path in written
    }
    assert relative_paths == _EXPECTED_ANATOMY_FILES


def test_generated_python_files_are_syntactically_valid(tmp_path: Path) -> None:
    written = generate_module("sample_module", dest_root=tmp_path)

    python_files = [path for path in written if path.suffix == ".py"]
    assert len(python_files) > 0
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))  # raises on bad syntax


def test_generator_refuses_to_clobber_an_existing_module(tmp_path: Path) -> None:
    generate_module("sample_module", dest_root=tmp_path)

    with pytest.raises(FileExistsError):
        generate_module("sample_module", dest_root=tmp_path)

    # force=True is an explicit opt-in to regenerate the template files.
    written_again = generate_module("sample_module", dest_root=tmp_path, force=True)
    assert len(written_again) == len(_EXPECTED_ANATOMY_FILES)


@pytest.mark.parametrize(
    "bad_name", ["Sample_Module", "sample-module", "1sample", "sample_", "_sample", ""]
)
def test_generator_rejects_names_that_are_not_lowercase_snake_case(
    tmp_path: Path, bad_name: str
) -> None:
    with pytest.raises(ValueError, match="snake_case"):
        generate_module(bad_name, dest_root=tmp_path)


def test_public_files_never_import_the_private_ones_in_the_generated_scaffold(
    tmp_path: Path,
) -> None:
    """The generator's own output must already satisfy the module-privacy
    rule it documents (MD-2/MD-3) -- api.py and contracts.py must not import
    models.py/schemas.py/repository.py/service.py.
    """
    generate_module("sample_module", dest_root=tmp_path)
    module_dir = tmp_path / "sample_module"
    private_modules = {"models", "schemas", "repository", "service"}

    for public_file in ("api.py", "contracts.py"):
        tree = ast.parse((module_dir / public_file).read_text(encoding="utf-8"))
        imported_names = {
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert imported_names.isdisjoint(private_modules)


def test_schema_name_follows_the_module_decomposition_aliases(tmp_path: Path) -> None:
    """A handful of modules have a schema name that differs from the module
    name (`Docs/10-architecture/04-module-decomposition.md` Sec.6, e.g.
    `identity_tenancy` -> `identity`) -- the generated migrations README must
    reflect the real schema name, not the module name verbatim.
    """
    generate_module("identity_tenancy", dest_root=tmp_path)
    readme = (tmp_path / "identity_tenancy" / "migrations" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "`identity` schema" in readme


def test_generated_scaffold_is_importable_as_a_real_package(tmp_path: Path) -> None:
    """Confirms the generated `__init__.py` files make the module a real,
    importable Python package -- not just files sitting on disk.
    """
    generate_module("sample_module", dest_root=tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("sample_module.api")
        assert module.__doc__ is not None
        assert "sample module" in module.__doc__
    finally:
        sys.path.remove(str(tmp_path))
        for name in list(sys.modules):
            if name == "sample_module" or name.startswith("sample_module."):
                del sys.modules[name]
