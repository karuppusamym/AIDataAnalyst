"""Bound a GraphQL document before anything executes it (R11-GQL01, design section 13).

A GraphQL request is a program the caller writes. Everything this module does
happens *before* a resolver runs -- no session is touched, no authorization gate
is asked, no row is read -- so a document that would be expensive is refused at
the cost of parsing it, not at the cost of running it. `tests/test_graphql_api.py`
asserts exactly that: a refused document executes zero database statements.

The checks, in the order they run, each cheaper than the next:

1. An operation name is required, and the document may hold exactly one
   operation, whose name must match. Multi-operation ambiguity is refused even
   when a name would disambiguate it: one document, one operation, one thing to
   budget. `query` operations are admitted, and a `mutation` only when it selects
   exactly one execution field (R11-GQL02) -- counted after fragments are expanded,
   so an alias, a second field or a spread cannot hide a second execution. There
   is no subscription.
2. The document is parsed with a token ceiling, so an oversized document stops
   being parsed rather than being parsed and then refused.
3. Fragment spreads are checked for cycles before anything walks them.
4. One walk over the operation, with every fragment expanded where it is
   spread, measures depth, alias count, page sizes, argument lengths and an
   upper bound on the number of objects the response could contain. Expanding
   fragments is what makes alias *repetition* visible: a fragment holding ten
   aliases spread five times is fifty aliases, however short the text is. The
   walk counts every selection it visits and gives up past a ceiling, so a
   document built to make the walk itself expensive is refused too.
5. graphql-core's specified validation rules run last, on a document already
   known to be small.

The node estimate is an upper bound, not a guess: every connection field
contributes its page size (`first`, or its schema default) as a multiplier on
everything beneath its `nodes`, and a field repeated under an alias counts once
per alias. The facade then returns at most that many objects, because a
connection never returns more than `first` nodes.

This module deliberately knows nothing about Atlas' types beyond one naming
convention: a type whose name ends in `Connection` is a page, and its `nodes`
field is the list the page size multiplies.

**Where the numbers come from.** `DEFAULT_LIMITS` is the design's initial budget.
A deployment tunes it through the `graphql_*` settings, read per request by
`limits_from_settings`; every setting is bounded, and its upper bound is the
matching `LIMIT_CEILINGS` value, which is where strawberry's backstop limiters
sit -- so no valid configuration makes a backstop refuse what admission passed.

**Introspection policy** (`introspection_decision`): off unless
`graphql_introspection_enabled`, which production refuses at startup; when on,
served only to `GRAPHQL_INTROSPECTION_ROLES`, and anyone else is refused
`INTROSPECTION_FORBIDDEN`. The published SDL is the supported discovery path in
every environment. Hiding the schema is not authorization: each field is decided
on its own whatever this policy says.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphql import (
    BooleanValueNode,
    DocumentNode,
    FieldNode,
    FloatValueNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLSyntaxError,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    NullValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    StringValueNode,
    ValueNode,
    VariableNode,
    get_named_type,
    parse,
    specified_rules,
    validate,
)

if TYPE_CHECKING:
    from atlas.platform.config import Settings

__all__ = [
    "DEFAULT_LIMITS",
    "DocumentCost",
    "DocumentRefused",
    "EXECUTION_ERROR_CODES",
    "GRAPHQL_INTROSPECTION_ROLES",
    "GraphQLLimits",
    "LIMIT_CEILINGS",
    "REFUSAL_CODES",
    "admit_document",
    "introspection_decision",
    "limits_from_settings",
]


@dataclass(frozen=True, slots=True)
class GraphQLLimits:
    """The demand budget for one GraphQL request.

    The design's initial values (depth 6, 50 aliases, page size at most 100,
    500 returned objects) plus the bounds it names without numbers: request and
    response bytes, scalar argument length and a resolver deadline. Frozen and
    passed explicitly -- built once per request from settings by
    `limits_from_settings` -- so a test can prove a limit by lowering it and
    nothing can loosen one while a request runs.
    """

    max_request_bytes: int = 32_768
    max_tokens: int = 2_000
    max_depth: int = 6
    max_aliases: int = 50
    max_page_size: int = 100
    max_nodes: int = 500
    max_string_argument_length: int = 512
    max_selection_visits: int = 5_000
    max_response_bytes: int = 1_048_576
    deadline_seconds: float = 10.0
    #: How many distinct datasources an organization-wide `tables` listing may
    #: authorize before it is refused as too broad. Each one is a gate decision,
    #: so this bounds the authorization work a single field can cause.
    max_scope_datasources: int = 200
    #: R11-GQL02: the most rows one execution mutation may ask for. Rows come back in
    #: the mutation's response once and are not retained, so this keeps a response
    #: inside `max_response_bytes` for ordinary widths rather than executing a query
    #: whose answer would then be withheld.
    max_execution_rows: int = 1_000
    #: Introspection is off by default: the schema is published as a versioned
    #: artifact under `Docs/90-reference/`, which is how a client discovers it.
    #: Hiding the schema is not treated as authorization -- every field is
    #: authorized on its own regardless of this flag.
    allow_introspection: bool = False
    #: The code a refused introspection carries: `INTROSPECTION_DISABLED` when the
    #: deployment serves none, `INTROSPECTION_FORBIDDEN` when it serves some but not
    #: to this caller (`introspection_decision`).
    introspection_refusal: str = "INTROSPECTION_DISABLED"


DEFAULT_LIMITS = GraphQLLimits()

#: The largest value each limit may be configured to: the upper bound of its `graphql_*`
#: setting (`tests/test_graphql_settings.py` holds the two together). strawberry's backstop
#: limiters are set here, so a deployment that raises a limit is never refused by a backstop
#: that still holds the old number.
LIMIT_CEILINGS = GraphQLLimits(
    max_request_bytes=1_048_576,
    max_tokens=20_000,
    max_depth=12,
    max_aliases=200,
    max_page_size=500,
    max_nodes=10_000,
    max_string_argument_length=4_096,
    max_selection_visits=50_000,
    max_response_bytes=16_777_216,
    deadline_seconds=120.0,
    max_scope_datasources=5_000,
    max_execution_rows=10_000,
)

#: Who may introspect when a deployment turns introspection on: the people who build against
#: the Agent Gateway. Everyone else reads the published SDL.
GRAPHQL_INTROSPECTION_ROLES: tuple[str, ...] = ("AgentDeveloper", "PlatformAdmin")


def introspection_decision(settings: Settings, roles: AbstractSet[str]) -> tuple[bool, str]:
    """Whether this caller may introspect here, and the refusal code if not.

    Off unless `graphql_introspection_enabled` -- which production refuses at startup, and
    which is also ignored here in production, so a settings object built around that
    validator cannot turn it on -- and then only for `GRAPHQL_INTROSPECTION_ROLES`.
    """
    if not settings.graphql_introspection_enabled or settings.environment == "production":
        return False, "INTROSPECTION_DISABLED"
    if set(roles).isdisjoint(GRAPHQL_INTROSPECTION_ROLES):
        return False, "INTROSPECTION_FORBIDDEN"
    return True, "INTROSPECTION_DISABLED"


def limits_from_settings(
    settings: Settings, *, roles: AbstractSet[str] = frozenset()
) -> GraphQLLimits:
    """One request's budget: the deployment's `graphql_*` settings, and the introspection
    decision for this caller. With default settings this is `DEFAULT_LIMITS`."""
    allowed, refusal = introspection_decision(settings, roles)
    return GraphQLLimits(
        max_request_bytes=settings.graphql_max_request_bytes,
        max_tokens=settings.graphql_max_tokens,
        max_depth=settings.graphql_max_depth,
        max_aliases=settings.graphql_max_aliases,
        max_page_size=settings.graphql_max_page_size,
        max_nodes=settings.graphql_max_nodes,
        max_string_argument_length=settings.graphql_max_string_argument_length,
        max_selection_visits=settings.graphql_max_selection_visits,
        max_response_bytes=settings.graphql_max_response_bytes,
        deadline_seconds=settings.graphql_deadline_seconds,
        max_scope_datasources=settings.graphql_max_scope_datasources,
        max_execution_rows=settings.graphql_max_execution_rows,
        allow_introspection=allowed,
        introspection_refusal=refusal,
    )

#: Every code a refused document can carry, with the HTTP status it is answered
#: with. Stable: clients and the published reference page key on these strings.
REFUSAL_CODES: dict[str, int] = {
    "REQUEST_TOO_LARGE": 413,
    "REQUEST_INVALID": 400,
    "BATCHING_NOT_SUPPORTED": 400,
    "OPERATION_NAME_REQUIRED": 400,
    "DOCUMENT_TOO_LARGE": 400,
    "DOCUMENT_INVALID": 400,
    "MULTIPLE_OPERATIONS": 400,
    "OPERATION_NOT_FOUND": 400,
    "OPERATION_NOT_SUPPORTED": 400,
    "EXECUTION_ROOT_INVALID": 400,
    # The caller's request or execution budget is spent (`aida.request_budget`); the
    # response carries Retry-After. Checked before the body is read, so it costs nothing.
    "RATE_LIMITED": 429,
    "FRAGMENT_CYCLE": 400,
    "INTROSPECTION_DISABLED": 400,
    # Introspection is on in this deployment, but not for this caller's roles.
    "INTROSPECTION_FORBIDDEN": 403,
    "DEPTH_LIMIT_EXCEEDED": 400,
    "ALIAS_LIMIT_EXCEEDED": 400,
    "PAGE_SIZE_EXCEEDED": 400,
    "ARGUMENT_TOO_LONG": 400,
    "NODE_BUDGET_EXCEEDED": 400,
    "DOCUMENT_TOO_COMPLEX": 400,
    "VALIDATION_FAILED": 400,
}

#: Codes an *admitted* document can meet while it executes, carried in
#: `errors[].extensions.code` of an HTTP 200 response. Field-level codes null
#: only the field that raised them; the last two withhold the whole `data`.
EXECUTION_ERROR_CODES: dict[str, str] = {
    "FORBIDDEN": (
        "the caller may not read this object; `extensions.reason` carries the value-free "
        "reason code the equivalent REST route puts in its 403"
    ),
    "NOT_FOUND": (
        "no such object (the REST route answers 404); reason COVERAGE_NOT_MEASURED when the "
        "routine or trigger exists but no parse has measured it"
    ),
    "GONE": (
        "the context product version was retired and this caller read it before; "
        "re-pin to the current published version (the REST route answers 410)"
    ),
    "INVALID_ARGUMENT": "an argument is out of range; `extensions.reason` says which rule",
    "INVALID_CURSOR": "`after` is not a cursor this field issued",
    "SCOPE_TOO_BROAD": "an organization-wide listing spans too many datasources; name one",
    "VALIDATION_FAILED": "a variable did not coerce to its declared type",
    "CONFLICT": (
        "R11-GQL02: the idempotency key was already used with different inputs, or the "
        "tool cannot run now (a quality hold, an unpublished version); for "
        "`contextProductCoverage`, the version names a table, routine or ontology version "
        "that no longer resolves (the REST route answers 409); `extensions.reason` says which"
    ),
    "REJECTED": "R11-GQL02: the gateway or parameter binding refused the execution",
    "EXECUTION_FAILED": "R11-GQL02: the source failed the execution; the receipt says so",
    "INTERNAL_ERROR": "an unexpected failure; the message is withheld, the correlation id is not",
    "DEADLINE_EXCEEDED": "execution passed the resolver deadline; no data is returned",
    "RESPONSE_TOO_LARGE": "the response passed the byte or object budget; no data is returned",
}

_INTROSPECTION_FIELDS = frozenset({"__schema", "__type"})
_PAGE_ARGUMENT = "first"
_CONNECTION_SUFFIX = "Connection"
_NODES_FIELD = "nodes"


class DocumentRefused(Exception):
    """A document refused before execution. Carries a stable code and a detail
    that describes the *document* -- never data, never an object name."""

    def __init__(self, code: str, detail: str, *, messages: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.messages = messages

    @property
    def status_code(self) -> int:
        return REFUSAL_CODES.get(self.code, 400)


@dataclass(frozen=True, slots=True)
class DocumentCost:
    """What the admitted document was measured at, reported back to the caller
    and into telemetry so limits can be calibrated from real traffic."""

    operation_name: str
    depth: int
    aliases: int
    estimated_nodes: int
    selections_visited: int
    #: "query" or "mutation": the endpoint dispatches an admitted document on it.
    operation_type: str = "query"
    #: The response keys of admitted `__schema`/`__type` fields. What they return is the
    #: schema itself -- bounded by the schema, not by data -- so the endpoint leaves them out
    #: of the returned-object count; the response byte ceiling still holds them.
    introspection_keys: tuple[str, ...] = ()


def admit_document(
    *,
    query: str,
    operation_name: str | None,
    variables: dict[str, Any] | None,
    schema: GraphQLSchema,
    limits: GraphQLLimits = DEFAULT_LIMITS,
) -> tuple[DocumentNode, DocumentCost]:
    """Parse, bound and validate one document, or raise `DocumentRefused`."""
    if not operation_name:
        raise DocumentRefused(
            "OPERATION_NAME_REQUIRED", "every request must name the operation it runs"
        )
    try:
        document = parse(query, no_location=False, max_tokens=limits.max_tokens)
    except RecursionError:
        # A deeply nested document can exhaust the parser before the AST depth
        # walk runs, even when its token count is below the ceiling.
        raise DocumentRefused(
            "DOCUMENT_TOO_COMPLEX", "the document exceeds parser nesting capacity"
        ) from None
    except GraphQLSyntaxError as error:
        if "tokens" in error.message and "Parsing aborted" in error.message:
            raise DocumentRefused(
                "DOCUMENT_TOO_LARGE", f"the document exceeds {limits.max_tokens} tokens"
            ) from None
        raise DocumentRefused(
            "DOCUMENT_INVALID",
            "the document is not valid GraphQL syntax",
            messages=(error.message,),
        ) from None

    operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if not operations:
        raise DocumentRefused("DOCUMENT_INVALID", "the document contains no operation")
    if len(operations) > 1:
        raise DocumentRefused("MULTIPLE_OPERATIONS", "a document may contain exactly one operation")
    operation = operations[0]
    if operation.name is None or operation.name.value != operation_name:
        raise DocumentRefused(
            "OPERATION_NOT_FOUND", "operationName does not name the operation in the document"
        )
    if operation.operation not in (OperationType.QUERY, OperationType.MUTATION):
        raise DocumentRefused(
            "OPERATION_NOT_SUPPORTED", "only query and mutation operations are served here"
        )

    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    _refuse_fragment_cycles(fragments)

    variable_values = dict(variables or {})
    _check_variable_strings(variable_values, limits)
    defaults: dict[str, ValueNode | None] = {
        definition.variable.name.value: definition.default_value
        for definition in operation.variable_definitions or ()
    }
    walk = _Walk(schema, fragments, variable_values, defaults, limits)
    if operation.operation is OperationType.MUTATION:
        root_type = schema.mutation_type
        if root_type is None:
            raise DocumentRefused("OPERATION_NOT_SUPPORTED", "the schema serves no mutations")
        _require_one_execution_root(operation.selection_set, fragments)
    else:
        root_type = schema.query_type
        if root_type is None:  # pragma: no cover - a schema without Query cannot be built
            raise DocumentRefused("OPERATION_NOT_SUPPORTED", "the schema serves no queries")
    walk.selection_set(operation.selection_set, root_type, depth=0, multiplier=1, page=None)

    errors: list[GraphQLError] = validate(schema, document, specified_rules, max_errors=10)
    if errors:
        raise DocumentRefused(
            "VALIDATION_FAILED",
            "the document does not validate against the schema",
            messages=tuple(error.message for error in errors),
        )
    return document, DocumentCost(
        operation_name=operation_name,
        depth=walk.max_depth_seen,
        aliases=walk.aliases,
        estimated_nodes=walk.nodes,
        selections_visited=walk.visits,
        operation_type="mutation" if operation.operation is OperationType.MUTATION else "query",
        introspection_keys=tuple(walk.introspection_keys),
    )


def _require_one_execution_root(
    selection_set: SelectionSetNode, fragments: dict[str, FragmentDefinitionNode]
) -> None:
    """A mutation selects exactly one field at its root, and it is not `__typename`.

    Counted with fragments and inline fragments expanded, and regardless of `@skip` or
    `@include`: a directive decided by a variable must not be the difference between one
    execution and two. Runs after the cycle check, so the expansion terminates.
    """
    roots: list[str] = []
    pending: list[SelectionSetNode] = [selection_set]
    while pending:
        current = pending.pop()
        for selection in current.selections:
            if isinstance(selection, FieldNode):
                roots.append(selection.name.value)
            elif isinstance(selection, InlineFragmentNode):
                pending.append(selection.selection_set)
            elif isinstance(selection, FragmentSpreadNode):
                fragment = fragments.get(selection.name.value)
                if fragment is not None:
                    pending.append(fragment.selection_set)
        if len(roots) > 1:
            break
    if len(roots) != 1 or roots[0].startswith("__"):
        raise DocumentRefused(
            "EXECUTION_ROOT_INVALID",
            "a mutation must select exactly one execution field: one execution per request",
        )


def _refuse_fragment_cycles(fragments: dict[str, FragmentDefinitionNode]) -> None:
    """Depth-first over spread edges; a back edge is a cycle. Iterative, so a
    long chain of fragments cannot exhaust the interpreter's stack."""
    edges = {name: _spreads(definition.selection_set) for name, definition in fragments.items()}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    for start in fragments:
        if state.get(start) == 2:
            continue
        stack: list[tuple[str, list[str]]] = [(start, list(edges.get(start, ())))]
        state[start] = 1
        while stack:
            name, pending = stack[-1]
            if not pending:
                state[name] = 2
                stack.pop()
                continue
            child = pending.pop()
            if child not in fragments:
                continue  # an undefined spread is a validation error, not a cycle
            if state.get(child) == 1:
                raise DocumentRefused("FRAGMENT_CYCLE", "a fragment spreads itself")
            if state.get(child) is None:
                state[child] = 1
                stack.append((child, list(edges.get(child, ()))))


def _spreads(selection_set: SelectionSetNode | None) -> list[str]:
    found: list[str] = []
    pending = [selection_set] if selection_set is not None else []
    while pending:
        current = pending.pop()
        for selection in current.selections:
            if isinstance(selection, FragmentSpreadNode):
                found.append(selection.name.value)
            elif isinstance(selection, FieldNode | InlineFragmentNode):
                if selection.selection_set is not None:
                    pending.append(selection.selection_set)
    return found


def _check_variable_strings(value: Any, limits: GraphQLLimits) -> None:
    """Every string anywhere in `variables` is held to the argument length bound."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if len(current) > limits.max_string_argument_length:
                raise DocumentRefused(
                    "ARGUMENT_TOO_LONG",
                    f"a variable exceeds {limits.max_string_argument_length} characters",
                )
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


class _Walk:
    """One pass over the operation with fragments expanded in place."""

    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: dict[str, FragmentDefinitionNode],
        variables: dict[str, Any],
        defaults: dict[str, ValueNode | None],
        limits: GraphQLLimits,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.variables = variables
        self.defaults = defaults
        self.limits = limits
        self.max_depth_seen = 0
        self.aliases = 0
        self.nodes = 0
        self.visits = 0
        self.introspection_keys: list[str] = []

    def selection_set(
        self,
        selection_set: SelectionSetNode | None,
        parent: GraphQLObjectType,
        *,
        depth: int,
        multiplier: int,
        page: int | None,
    ) -> None:
        if selection_set is None:
            return
        for selection in selection_set.selections:
            self.visits += 1
            if self.visits > self.limits.max_selection_visits:
                raise DocumentRefused(
                    "DOCUMENT_TOO_COMPLEX",
                    f"the document expands past {self.limits.max_selection_visits} selections",
                )
            if isinstance(selection, FieldNode):
                self.field(selection, parent, depth=depth, multiplier=multiplier, page=page)
            elif isinstance(selection, InlineFragmentNode):
                target = parent
                if selection.type_condition is not None:
                    named = self.schema.get_type(selection.type_condition.name.value)
                    if isinstance(named, GraphQLObjectType):
                        target = named
                self.selection_set(
                    selection.selection_set, target, depth=depth, multiplier=multiplier, page=page
                )
            elif isinstance(selection, FragmentSpreadNode):
                fragment = self.fragments.get(selection.name.value)
                if fragment is None:
                    continue  # reported by validation
                named = self.schema.get_type(fragment.type_condition.name.value)
                target = named if isinstance(named, GraphQLObjectType) else parent
                self.selection_set(
                    fragment.selection_set, target, depth=depth, multiplier=multiplier, page=page
                )

    def field(
        self,
        node: FieldNode,
        parent: GraphQLObjectType,
        *,
        depth: int,
        multiplier: int,
        page: int | None,
    ) -> None:
        name = node.name.value
        depth += 1
        self.max_depth_seen = max(self.max_depth_seen, depth)
        if depth > self.limits.max_depth:
            raise DocumentRefused(
                "DEPTH_LIMIT_EXCEEDED", f"the document nests deeper than {self.limits.max_depth}"
            )
        if node.alias is not None:
            self.aliases += 1
            if self.aliases > self.limits.max_aliases:
                raise DocumentRefused(
                    "ALIAS_LIMIT_EXCEEDED",
                    f"the document uses more than {self.limits.max_aliases} aliases",
                )
        if name in _INTROSPECTION_FIELDS:
            if not self.limits.allow_introspection:
                refusal = self.limits.introspection_refusal
                raise DocumentRefused(
                    refusal,
                    "introspection is disabled; the schema is published as a versioned artifact"
                    if refusal == "INTROSPECTION_DISABLED"
                    else "introspection is served to PlatformAdmin and AgentDeveloper only; "
                    "the schema is published as a versioned artifact",
                )
            self.introspection_keys.append(node.alias.value if node.alias else name)
        if name.startswith("__"):
            return  # __typename, and introspection when allowed: no data objects
        definition = parent.fields.get(name)
        if definition is None:
            return  # unknown field: reported by validation
        self._check_string_arguments(node)
        child_page = self._page_size(node, definition)
        returned = get_named_type(definition.type)
        if not isinstance(returned, GraphQLObjectType):
            return  # a scalar or enum leaf
        if name == _NODES_FIELD and parent.name.endswith(_CONNECTION_SUFFIX) and page is not None:
            multiplier *= page
        self.nodes += multiplier
        if self.nodes > self.limits.max_nodes:
            raise DocumentRefused(
                "NODE_BUDGET_EXCEEDED",
                f"the document could return more than {self.limits.max_nodes} objects",
            )
        self.selection_set(
            node.selection_set, returned, depth=depth, multiplier=multiplier, page=child_page
        )

    def _page_size(self, node: FieldNode, definition: GraphQLField) -> int | None:
        """The page this field asks for, when it returns a connection."""
        returned = get_named_type(definition.type)
        if not (
            isinstance(returned, GraphQLObjectType)
            and returned.name.endswith(_CONNECTION_SUFFIX)
            and _PAGE_ARGUMENT in definition.args
        ):
            return None
        supplied: Any = None
        for argument in node.arguments:
            if argument.name.value == _PAGE_ARGUMENT:
                supplied = self._value(argument.value)
        if supplied is None:
            supplied = definition.args[_PAGE_ARGUMENT].default_value
        if not isinstance(supplied, int) or isinstance(supplied, bool):
            # Not an integer: validation or the resolver refuses it. Budget the
            # worst case so an unparseable page size can never undercount.
            return self.limits.max_page_size
        if supplied > self.limits.max_page_size:
            raise DocumentRefused(
                "PAGE_SIZE_EXCEEDED", f"a page may hold at most {self.limits.max_page_size} items"
            )
        return max(supplied, 0)

    def _check_string_arguments(self, node: FieldNode) -> None:
        for argument in node.arguments:
            pending: list[ValueNode] = [argument.value]
            while pending:
                value = pending.pop()
                if isinstance(value, StringValueNode):
                    if len(value.value) > self.limits.max_string_argument_length:
                        raise DocumentRefused(
                            "ARGUMENT_TOO_LONG",
                            "an argument exceeds "
                            f"{self.limits.max_string_argument_length} characters",
                        )
                elif isinstance(value, ListValueNode):
                    pending.extend(value.values)
                elif isinstance(value, ObjectValueNode):
                    pending.extend(field.value for field in value.fields)

    def _value(self, node: ValueNode) -> Any:
        if isinstance(node, IntValueNode):
            return int(node.value)
        if isinstance(node, VariableNode):
            name = node.name.value
            if name in self.variables:
                return self.variables[name]
            default = self.defaults.get(name)
            return self._value(default) if default is not None else None
        if isinstance(node, NullValueNode):
            return None
        if isinstance(node, FloatValueNode | StringValueNode | BooleanValueNode):
            return node.value
        return None
