"""R11-GQL01 (GQL-E): the Agent Gateway explorer's example operations cannot rot either.

`ui-next/src/components/GraphqlExplorer.tsx` ships four hand-written operations that a person
can run against `POST /graphql` with one click. `tests/test_graphql_examples.py` holds the
*documentation* page's examples to the served schema; these are a second copy, in TypeScript, and
nothing held them to anything -- a renamed field would have turned the explorer's default query
into a validation error that no test saw.

Read here, not copied: the explorer's source is parsed for each example's id, operation name,
query text and variables, and every one is admitted against the served schema at the default
limits exactly as the endpoint admits a document (parse, one named operation, validation, depth,
aliases, page sizes, object budget). A source that stops parsing into examples fails loudly
rather than passing on an empty list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from graphql import OperationDefinitionNode

from aida.graphql_limits import DEFAULT_LIMITS, admit_document
from aida.graphql_schema import metadata_schema

EXPLORER = (
    Path(__file__).resolve().parents[1]
    / "ui-next"
    / "src"
    / "components"
    / "GraphqlExplorer.tsx"
)

_ARRAY = re.compile(r"const EXAMPLES[^=]*=\s*\[(?P<body>.*?)\n\];", re.DOTALL)
_ENTRY = re.compile(r"\n  \{\n(?P<entry>.*?)\n  \},?(?=\n  \{|\n?$)", re.DOTALL)
_ID = re.compile(r'\bid: "(?P<value>[^"]+)"')
_OPERATION = re.compile(r'\boperationName: "(?P<value>[^"]+)"')
_QUERY = re.compile(r"\bquery: `(?P<value>[^`]*)`", re.DOTALL)
_VARIABLES = re.compile(r"\bvariables: (?P<value>.*)$", re.DOTALL)


@dataclass(frozen=True)
class Example:
    id: str
    operation_name: str
    query: str
    variables: dict[str, Any]


def _ts_object_to_json(text: str) -> dict[str, Any]:
    """The one shape the explorer uses: `() => ({ ... })` or `(projectId) => ({ ... })`, with
    bare keys, double-quoted strings, trailing commas and a `projectId ?? "X"` default."""
    body = re.sub(r"^\s*\(?\w*\)?\s*=>\s*\(", "", text.strip())
    body = body.rstrip().rstrip(",").rstrip()
    assert body.endswith(")"), text
    body = body[:-1]
    body = re.sub(r'projectId \?\? ("[^"]*")', r"\1", body)
    body = re.sub(r"(?<=[{,\s])([A-Za-z_]\w*)\s*:", r'"\1":', body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    return parsed


def _examples() -> list[Example]:
    source = EXPLORER.read_text(encoding="utf-8")
    array = _ARRAY.search(source)
    assert array is not None, "the EXAMPLES array was not found in GraphqlExplorer.tsx"
    examples: list[Example] = []
    for entry in _ENTRY.finditer("\n" + array.group("body") + "\n"):
        text = entry.group("entry")
        example_id, operation, query, variables = (
            _ID.search(text),
            _OPERATION.search(text),
            _QUERY.search(text),
            _VARIABLES.search(text),
        )
        assert example_id and operation and query and variables, text[:120]
        examples.append(
            Example(
                id=example_id.group("value"),
                operation_name=operation.group("value"),
                query=query.group("value"),
                variables=_ts_object_to_json(variables.group("value")),
            )
        )
    return examples


EXAMPLES = _examples()


def test_the_explorer_still_ships_its_four_examples() -> None:
    # Parsing must not silently find fewer: an empty list would pass every test below.
    assert [example.id for example in EXAMPLES] == [
        "datasources",
        "context-products",
        "lineage-impact",
        "execute-tool",
    ]


@pytest.mark.parametrize("example", EXAMPLES, ids=[example.id for example in EXAMPLES])
def test_each_explorer_example_is_admitted_at_the_default_limits(example: Example) -> None:
    document, cost = admit_document(
        query=example.query,
        operation_name=example.operation_name,
        variables=example.variables,
        schema=metadata_schema._schema,
        limits=DEFAULT_LIMITS,
    )
    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    assert [operation.name.value for operation in operations if operation.name] == [
        example.operation_name
    ]
    assert cost.estimated_nodes <= DEFAULT_LIMITS.max_nodes


def test_only_the_execute_example_is_a_mutation() -> None:
    # The explorer gates a mutation behind an explicit confirmation by *looking at the query*
    # (`isMutation`); an example that stopped starting with `mutation` would run unconfirmed.
    kinds = {example.id: example.query.lstrip().split()[0] for example in EXAMPLES}
    assert kinds == {
        "datasources": "query",
        "context-products": "query",
        "lineage-impact": "query",
        "execute-tool": "mutation",
    }


def test_the_parser_reads_variables_including_a_nested_object() -> None:
    by_id = {example.id: example for example in EXAMPLES}
    assert by_id["datasources"].variables == {"first": 20}
    assert by_id["context-products"].variables == {"projectId": "PROJECT_ID", "first": 20}
    request = by_id["execute-tool"].variables["request"]
    assert request["maxRows"] == 100 and request["parameters"] == {}
