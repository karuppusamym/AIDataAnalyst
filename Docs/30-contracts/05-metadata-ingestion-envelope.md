# Metadata Ingestion Envelope

> Status: Authoritative, T1 external contract. Owner: Data Platform. Current version: `1.1`. **`1.0` remains accepted, unchanged, permanently.**
> One envelope for every transport: native pull, authenticated push, source-side agent, and future broker intake (ADR-0012).

> **Implementation status (2026-08-30).** `1.1` is implemented for the view, routine, object-comment and grant axes (`src/aida/schemas.py`, `src/aida/envelope_models.py`, `src/aida/ingestion.py`, migration `a1c9f4b7e230`). Index and partition inventory, listed against 1.1 in earlier drafts of §10, are **not** delivered and remain tracker `CN-8`. Native pull for the new axes is implemented in **all five** connectors, with one honest exception: BigQuery advertises `grants: false` because BigQuery has no SQL grants (INV-9). The push transport accepts 1.1 from any producer regardless of connector. **The pull path does not yet persist the new axes** — `activities.discover_datasource` calls `persist_discovery_snapshot` but not `persist_envelope_extensions`, so today only the two push paths store them. Detail and evidence: `Docs/review-2026-08/gap/07-envelope-v11.md` (contract, storage, PostgreSQL, SQL Server) and `Docs/review-2026-08/gap/08-envelope-v11-connectors.md` (Oracle, Snowflake, BigQuery).

## 1. Why one envelope

Four transports, one persistence path. Object identity, drift detection, privacy filtering, and graph projection behave identically regardless of how metadata arrived — so a fix applies everywhere, and a new transport is an adapter rather than a new persistence design.

## 2. Envelope 1.0

```json
{
  "envelope_version": "1.0",
  "idempotency_key": "cmdb:2026-08-27:0001",
  "producer": "bank-metadata-bridge",
  "transport": "PUSH",
  "snapshot_type": "INCREMENTAL",
  "emitted_at": "2026-08-27T20:00:00Z",
  "catalogs": [
    {
      "name": "bank",
      "attributes": {"region": "us-east"},
      "schemas": [
        {
          "name": "customer",
          "tables": [
            {
              "name": "account",
              "object_type": "BASE_TABLE",
              "columns": [
                {
                  "name": "account_id",
                  "ordinal_position": 1,
                  "physical_type": "bigint",
                  "nullable": false
                }
              ],
              "constraints": [
                {
                  "name": "account_pk",
                  "constraint_type": "PRIMARY_KEY",
                  "columns": ["account_id"]
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

## 2.1 Envelope 1.1 — the four axes it adds

1.1 is **purely additive**. Every field below is optional, every 1.0 payload validates unchanged, and a producer written against 1.0 keeps working with no edit, forever. `envelope_version` also still *defaults* to `"1.0"`, so a producer that never sent the field is not silently promoted.

The axes exist because four questions could not be answered from a 1.0 snapshot: what a view is actually computed from, what a stored procedure does, what the source's own authors wrote about an object, and who the source already lets read it. The first two are the input to view-DDL lineage parsing and procedure parsing; the third is the strongest meaning signal an estate carries for free; the fourth is evidence for reviewing a workspace source binding.

```json
{
  "envelope_version": "1.1",
  "idempotency_key": "cmdb:2026-08-30:0001",
  "producer": "bank-metadata-bridge",
  "transport": "PUSH",
  "snapshot_type": "INCREMENTAL",
  "emitted_at": "2026-08-30T20:00:00Z",
  "catalogs": [
    {
      "name": "bank",
      "source_description": "the consumer banking warehouse",
      "schemas": [
        {
          "name": "customer",
          "source_description": "deposit subject area",
          "tables": [
            {
              "name": "open_account",
              "object_type": "VIEW",
              "source_description": "accounts that are still open",
              "view_definition": {
                "definition_sql": "SELECT account_id FROM customer.account WHERE closed_on IS NULL",
                "is_materialized": false,
                "is_updatable": true,
                "check_option": "NONE",
                "truncated": false,
                "unavailable_reason": null
              },
              "columns": [
                {
                  "name": "account_id",
                  "ordinal_position": 1,
                  "physical_type": "bigint",
                  "nullable": false,
                  "source_description": "surrogate key of the deposit account"
                }
              ]
            }
          ],
          "routines": [
            {
              "name": "close_account",
              "routine_type": "PROCEDURE",
              "language": "plpgsql",
              "body_sql": "BEGIN UPDATE customer.account SET closed_on = now() WHERE account_id = p_account_id; END;",
              "return_type": null,
              "is_deterministic": false,
              "security_mode": "DEFINER",
              "source_description": null,
              "truncated": false,
              "unavailable_reason": null,
              "parameters": [
                {
                  "name": "p_account_id",
                  "ordinal_position": 1,
                  "mode": "IN",
                  "physical_type": "bigint",
                  "default_expression": null
                }
              ],
              "attributes": {}
            }
          ],
          "grants": [
            {
              "grantee": "risk_reader",
              "grantee_type": "ROLE",
              "privilege": "SELECT",
              "object_type": "TABLE",
              "object_name": "account",
              "schema_name": "customer",
              "is_grantable": false
            }
          ]
        }
      ]
    }
  ]
}
```

### New field semantics

| Field | Where | Required | Semantics |
|---|---|:--:|---|
| `source_description` | catalog, schema, table, column | No | The description the **source** carries. Evidence, not authority: a steward-authored or model-proposed description outranks it. ≤ 10,000 chars |
| `view_definition` | table | No | Present only for views and materialized views. See below |
| `view_definition.definition_sql` | | No | The defining text, verbatim. `null` means **unavailable**, never empty |
| `view_definition.is_materialized` | | No | Default `false` |
| `view_definition.is_updatable` | | No | Tri-state: `true` \| `false` \| `null` (source did not say) |
| `view_definition.check_option` | | No | `NONE` \| `LOCAL` \| `CASCADED`, source-reported |
| `view_definition.truncated` | | No | `true` if the source returned a prefix. Default `false` |
| `view_definition.unavailable_reason` | | Conditional | **Required when `definition_sql` is `null`; forbidden otherwise** |
| `routines[]` | schema | No | Stored procedures and functions. ≤ 10,000 per schema, ≤ 50,000 per envelope |
| `routines[].routine_type` | | Yes | `FUNCTION` \| `PROCEDURE` |
| `routines[].body_sql` | | No | The body, verbatim. `null` means **unavailable**, never empty |
| `routines[].unavailable_reason` | | Conditional | **Required when `body_sql` is `null`; forbidden otherwise** |
| `routines[].security_mode` | | No | `DEFINER` \| `INVOKER` |
| `routines[].parameters[]` | | No | Ordered; `ordinal_position` unique within a routine; `mode` is `IN` \| `OUT` \| `INOUT` \| `VARIADIC` \| `TABLE` |
| `routines[].attributes` | | No | Same bounds and same value-free screening as every other attribute bag (§7) |
| `grants[]` | schema | No | Source-side privileges. ≤ 100,000 per schema |
| `grants[].grantee_type` | | No | `USER` \| `ROLE` \| `GROUP` \| `PUBLIC`. Default `ROLE` |
| `grants[].object_type` | | No | `TABLE` \| `VIEW` \| `PROCEDURE` \| `FUNCTION` \| `SCHEMA` \| `SEQUENCE`. Default `TABLE` |
| `grants[].is_grantable` | | No | `WITH GRANT OPTION`. Default `false` |

### Unavailable is not empty

The single rule the 1.1 storage model is shaped around:

| The source… | `definition_sql` / `body_sql` | `truncated` | `unavailable_reason` |
|---|---|:--:|---|
| gave the full text | the text | `false` | `null` |
| gave a prefix | the prefix | `true` | `null` |
| **would not give it** | **`null`** | `false` | **required** |
| has nothing to give | `""` | `false` | `null` |

A null definition with no reason is **rejected**, not accepted. An unexplained null is indistinguishable from a connector defect six months later, and a downstream parser that cannot tell "not allowed to read it" from "there is nothing to read" reports a confident absence of lineage for a view it never saw.

### Grants are evidence, never authority

Nothing in `grants[]` grants anything in this platform. The policy engine does not read it, no authorization decision consults it, and ADR-0018 keeps authority in the platform's own access policies. The axis exists so that "who can already see this in the source" is answerable and so a workspace source binding can be reviewed against what the source itself permits.

`DENY`-style negative privileges are **not** modelled. Representing revocation would need a resolution rule that nothing downstream consumes yet, and a half-represented DENY reads as an absent one.

### Version discipline

| Case | Behaviour |
|---|---|
| `envelope_version: "1.0"`, no 1.1 fields | Accepted, behaves exactly as before. Permanent |
| `envelope_version: "1.1"`, any content | Accepted |
| `envelope_version: "1.0"` **carrying 1.1 fields** | **422**, naming every offending field |
| `envelope_version` omitted | Treated as `"1.0"` |
| Batch chunks | Carry no version of their own; validated against the manifest's `envelope_version` at upload |

Declaring 1.0 while sending 1.1 content is rejected rather than silently stripped. A producer that ships view definitions and receives `201` has every reason to expect lineage to follow, and would discover otherwise only months later by noticing an absence.

## 3. Field semantics

| Field | Required | Semantics |
|---|:--:|---|
| `envelope_version` | Yes | Contract version. Backward-compatible evolution only. |
| `idempotency_key` | Yes | Unique per datasource. Same key + same payload → original job. Same key + different payload → **409**. |
| `producer` | Yes | Producer identity. Signed producer identity is planned. |
| `transport` | Yes | `PULL` \| `PUSH` \| `AGENT` \| `STREAM` |
| `snapshot_type` | Yes | `FULL` \| `INCREMENTAL` |
| `emitted_at` | Yes | Producer-side timestamp (RFC 3339 UTC) |
| `catalogs[]` | Yes | Nested inventory |
| `attributes` | No | Scalar, bounded, ≤ 50 per object |

## 4. Snapshot semantics — the part that matters most

| Type | Behaviour |
|---|---|
| `INCREMENTAL` | Creates and updates objects present in the envelope. **Never retires omitted objects.** Safe default. |
| `FULL` | Authoritative for the complete datasource scope. Soft-deprecates active objects omitted from the envelope. **Requires explicit confirmation** in the UI. |

**A `FULL` 1.0 envelope is authoritative for the 1.0 inventory only.** It carries no statement about views, routines, descriptions or grants, so its silence is not omission and the 1.1 axes are left alone. Reconciliation of the 1.1 axes is gated on the declared version *and* on `FULL`, so a producer that rolls back to 1.0 for a release does not wipe the estate's view definitions. The same chunk accumulation rule below applies to the 1.1 axes, through the same mechanism.

**The critical rule for batched `FULL`:** a `FULL` batch accumulates stable object identities across every chunk and runs omission reconciliation **only after all chunks have succeeded**. It can never retire metadata from a partial delivery.

Without this rule, a network failure halfway through a large `FULL` delivery would soft-delete the metadata that did not arrive — data loss caused by a transient error. This is the single most important correctness property in the ingestion path.

## 5. Atomicity and locking

| Property | Behaviour |
|---|---|
| Lock | Deliveries acquire a datasource row lock, serializing competing snapshots for one source without blocking others |
| Atomicity | Delivery record, catalog changes, and the graph snapshot event commit in one transaction |
| Fingerprints | SHA-256 over canonical JSON |
| Payload retention | Raw payloads are **not retained** in the ingestion job after success |

## 6. Bounds

| Boundary | Default | Adjustable |
|---|---|---|
| Synchronous envelope | 100 catalogs / 50,000 tables / 250,000 columns / 50,000 routines | Down only |
| Batch | 1,000 chunks / 1,000,000 tables / 5,000,000 columns | Down only |
| Attributes per object | 50, scalar, bounded | Down only |
| Request size (local proxy) | 40 MiB | — |

Larger estates use the durable batch contract (§8). Cumulative table and column admission is enforced **under the batch lock during every upload** and rechecked before processing — so a batch cannot exceed its bound by racing uploads.

## 7. Validation and privacy

Validated before persistence: nested names, sizes, object types, constraint types, foreign-key cardinality, local-column references, duplicate columns, duplicate ordinals.

Additionally for 1.1: routine type and parameter mode enumerations, duplicate routine-parameter ordinals, grantee and privilege shape, the availability rule above (a null definition or body must carry a reason; a reason must not accompany a present one), and the declared-version rule. A malformed 1.1 field is a **422**, never a silently dropped field.

**Rejected outright:** attribute keys associated with samples, row values, passwords, secrets, tokens, or credentials (INV-6).

Technical default expressions and descriptions are permitted but bounded. **Producers remain responsible for excluding literal regulated values** — the platform rejects what it can detect, but a description field containing a customer name is a producer defect.

## 8. Durable batch contract

For estates above the synchronous boundary.

```mermaid
sequenceDiagram
    participant P as Producer
    participant A as Atlas
    participant T as Temporal

    P->>A: POST batches (manifest, expected_chunks)
    A-->>P: batch_id
    loop 1..expected_chunks
      P->>A: POST chunks (number, key, checksum, payload)
      A-->>P: chunk accepted
    end
    P->>A: POST finalize
    A->>A: verify exact sequence 1..N
    A->>T: submit workflow
    T->>A: process chunks (independent commits, heartbeats)
    T->>A: cross-chunk FK resolution pass
    T->>A: FULL reconciliation (only if all chunks succeeded)
    A->>A: clear payload JSON (SQL NULL)
    P->>A: GET batch (progress or failure evidence)
```

| Rule | Detail |
|---|---|
| Batch key | Unique per datasource |
| Chunk number and key | Unique within the batch |
| Replay | Exact replay returns original records; reuse with different content → 409 |
| Finalization | Allowed only when the exact sequence `1..expected_chunks` exists |
| Independence | Chunks commit independently, so retry resumes rather than restarts |
| Idempotency | Object fingerprints make reapplication safe |
| Cross-chunk FKs | A second value-free pass resolves FKs whose referenced table arrived in another chunk |
| Failure | Validated chunk payloads retained for authorized retry; replacement run linked via `resumed_from_run_id` |
| Temporal unavailable | **Fails closed** — no stranded pseudo-queued job |

## 9. API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/connectors/capability-matrix` | Honest implementation, maturity, transport, version inventory |
| POST | `/v1/datasources/{id}/metadata-ingestions` | Validate and atomically apply an envelope |
| GET | `/v1/datasources/{id}/metadata-ingestions` | Delivery and change evidence |
| POST | `/v1/datasources/{id}/metadata-ingestion-batches` | Create an idempotent manifest |
| GET | `/v1/datasources/{id}/metadata-ingestion-batches` | Batch progress and completion evidence |
| POST | `/v1/metadata-ingestion-batches/{id}/chunks` | Upload a checksum-addressed chunk |
| GET | `/v1/metadata-ingestion-batches/{id}/chunks` | Chunk status and checksums — **payload never exposed** |
| POST | `/v1/metadata-ingestion-batches/{id}/finalize` | Seal and submit |
| GET | `/v1/metadata-ingestion-batches/{id}` | Poll workflow progress or failure evidence |
| POST | `/v1/datasources/{id}/connector-certifications` | Persist conformance evidence |
| GET | `/v1/datasources/{id}/connector-certifications` | Certification history |

Roles: `PlatformAdmin`, `MetadataAdmin`, `DataAdmin`, or the workload-oriented `MetadataIngestor`. All mutations write audit and outbox records.

## 10. Planned evolution

| Version | Adds | State |
|---|---|---|
| 1.1 | View definitions; routines and their parameters; source-side object descriptions; source-side grants | **Shipped 2026-08-30** |
| 1.1+ | Index and partition inventory — deferred out of 1.1, tracked as `CN-8` | Not started |
| 1.2 | BI assets (dashboards, reports); pipeline and topic assets | Not started |
| 1.3 | File and API assets; ML model assets | Not started |
| 2.0 | Only if a breaking change becomes unavoidable | — |

All evolution is backward-compatible within a major version, governed by the schema-registry compatibility policy.

## Related documents

- Envelope 1.1 record and evidence: `review-2026-08/gap/07-envelope-v11.md`
- Ingestion module: `20-modules/03-ingestion.md`
- ADR-0012: `10-architecture/adr/ADR-0012-single-metadata-envelope.md`
