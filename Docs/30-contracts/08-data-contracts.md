# Data Contracts

> Status: Proposed. Owner: Data Governance.
> A *data contract* is an agreement between a data producer and its consumers about shape, semantics, quality, and change. Distinct from the API contracts in this folder, which govern Atlas's own interfaces.

## 1. Why Atlas should have these

Collibra ships **Data Contract registries** and **Data Product registries**; Alation ships a Data Products Marketplace; Atlan ships a Data Marketplace. This is now an expected capability in the segment.

More importantly, it fits Atlas's architecture unusually well. Every other vendor's data contract is a *document* — a registered agreement that something checks periodically. Atlas can make it **enforceable at runtime**, because Atlas is in the query path:

| Contract clause | Elsewhere | In Atlas |
|---|---|---|
| Schema stability | A registered promise; drift detected later | Drift detection already exists; a breach can gate consumers |
| Quality thresholds | A separate monitoring tool | The quality module's policies *are* the clause |
| Freshness SLA | A dashboard | Feeds the runtime trust signal |
| Semantic meaning | A description field | An approved, versioned semantic annotation |
| Change notice | An email | Impact analysis plus a maker-checker gate on the producer's change |

That is whitespace adjacent to W1, and it reuses machinery that already exists.

## 2. Contract shape

```json
{
  "id": "dc_positions_v2",
  "version": 2,
  "producer": {"owner": "markets-data-eng", "asset": "tbl_positions"},
  "consumers": [
    {"principal": "risk-analytics", "purpose": "regulatory_reporting"}
  ],
  "schema": {
    "guaranteed_columns": [
      {"name": "position_id", "type": "string", "nullable": false},
      {"name": "as_of_date", "type": "date", "nullable": false},
      {"name": "exposure_amount", "type": "decimal", "nullable": false}
    ],
    "stability": "BREAKING_CHANGE_REQUIRES_APPROVAL"
  },
  "semantics": {"semantic_version": 44, "grain": "one row per position per as_of_date"},
  "quality": {
    "max_null_rate": {"exposure_amount": 0.0},
    "min_row_count": 1000,
    "freshness_sla_hours": 24
  },
  "change_policy": {
    "notice_period_days": 30,
    "requires_consumer_acknowledgement": true
  },
  "status": "ACTIVE"
}
```

## 3. Enforcement points

| Point | Behaviour |
|---|---|
| Producer change | A change violating `schema.stability` raises a proposal requiring approval and consumer notice |
| Quality breach | Opens an incident **attributed to the contract**, notifying consumers named in it |
| Freshness breach | Sets the trust signal and warns consumers at query time |
| Semantic change | Superseding the pinned semantic version triggers consumer notification |
| Consumer onboarding | Registering as a consumer records the dependency for future impact analysis |

## 4. Relationship to existing modules

This is **composition, not a new subsystem**. That is what makes it cheap.

| Clause | Implemented by |
|---|---|
| Schema guarantees | 04 catalog (fingerprints, drift) |
| Quality thresholds | 11 data-quality (policies, incidents) |
| Freshness | 11 data-quality (watermark contracts) |
| Semantics | 07 semantic-layer (versioned annotations) |
| Change approval | 17 policy-governance (maker-checker) |
| Consumer notification | 20 observability-audit (events) |
| Impact | 09 lineage |

A data contract is a **named bundle** of clauses over these modules, plus a producer/consumer relationship and a change policy.

## 5. Data products

A data product is a curated, owned, documented, discoverable bundle of assets with a data contract attached.

```text
data_product
├── owner and steward
├── included assets
├── data contract (this document)
├── semantic annotations
├── quality posture
├── access policy
└── eligible governed tools
```

Note the last line. A data product with **eligible tools** is close to a context product (module 19) aimed at humans rather than at agents — and the two should share the same underlying definition rather than being built twice.

## 6. Open questions

| # | Question | Blocks |
|---|---|---|
| DC-1 | Are data products and context products one concept with two surfaces, or two concepts? | Module 19 design |
| DC-2 | Does a contract breach block consumers, or only warn them? | Runtime coupling design |
| DC-3 | Who arbitrates when a producer must break a contract? | Governance operating model |
| DC-4 | Is the contract versioned independently of the asset? | Data model |

## 7. Priority

**P2.** This is a Phase C item. It composes existing machinery, so it should not be started until quality (11), glossary (08), and context products (19) are delivered — building it earlier means building the clauses twice.

## Related documents

- Data quality: `20-modules/11-data-quality.md`
- Context products: `20-modules/19-context-products-and-mcp.md`
- Policy and governance: `20-modules/17-policy-and-governance.md`
