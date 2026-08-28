# Sources

> Status: Reference. Owner: Product.
> External sources consulted for the competitive analysis in `00-product/03-market-landscape.md` and `00-product/04-competitive-feature-matrix.md`.

## Research baseline

**Date:** 2026-08-28.

**Boundary.** This research compares Atlas to **vendor-stated public positioning** — product pages, engineering blogs, and analyst-review summaries. It does not compare against private roadmaps, customer-specific deployments, or non-public capability. Where a vendor describes a capability as preview or private preview, that qualifier is carried into the analysis.

**Bias to be aware of.** Vendor product pages overstate. The matrix is therefore deliberately generous to competitors and honest about Atlas — a competitor claim is scored as stated, while Atlas is scored against `60-delivery/04-status-matrix.md`. A capability Atlas scores `◐` on may be scored `●` for a competitor on weaker evidence. This asymmetry is intentional: it prevents the analysis flattering the home team.

## Primary vendor sources

### Independent governance platforms

- [Atlan — The Context Layer for AI](https://atlan.com/)
- [Alation — Agentic Data Intelligence Platform](https://www.alation.com/product/agentic-data-intelligence-platform/)
- [Collibra Platform](https://www.collibra.com/products/collibra-platform)
- [Microsoft Purview Data Governance](https://www.microsoft.com/en-us/security/business/risk-management/microsoft-purview-data-governance)

### Warehouse-native context planes

- [What's new with Unity Catalog at Data + AI Summit 2026](https://www.databricks.com/blog/whats-new-unity-catalog-data-ai-summit-2026)
- [Unity Catalog](https://www.databricks.com/product/unity-catalog)
- [Snowflake Horizon Context: The Governed Context Layer for AI, BI and Apps](https://www.snowflake.com/en/blog/horizon-context-governed-context/)
- [Snowflake Horizon Catalog announcement](https://www.snowflake.com/en/news/press-releases/snowflake-advances-trusted-ai-with-snowflake-horizon-catalog-centralizing-governance-context-and-security-across-the-enterprise/)
- [Cortex Analyst documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst)

### Semantic layer and AI analytics

- [Semantic Layer vs. Text-to-SQL: 2026 Benchmark Update — dbt](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)
- [Best Semantic Layer for AI and BI in 2026 — Cube](https://cube.dev/articles/best-semantic-layer-for-ai-and-bi-2026)
- [Text-to-SQL for Enterprise: Metric Drift and Context Layer — Atlan](https://atlan.com/know/ai-agent/data-for-ai/text-to-sql-for-enterprise/)

### Quality and observability

- [Data Observability Tools: Key Features & Top Solutions — Dagster](https://dagster.io/learn/data-observability-tools)
- [Top Data Observability Tools — Atlan](https://atlan.com/know/data-observability-tools/)

### Open source

- [OpenMetadata vs. DataHub — Atlan](https://atlan.com/openmetadata-vs-datahub/)
- [Open Source Data Catalog Tools — Atlan](https://atlan.com/open-source-data-catalog-tools/)

### Analyst and review aggregators

- [Alation Agentic Data Intelligence Platform — Gartner Peer Insights](https://www.gartner.com/reviews/market/metadata-management-solutions/vendor/alation/product/alation-agentic-data-intelligence-platform)

## How to refresh this research

The competitive picture in this segment moves on roughly a quarterly cadence, driven by vendor conferences.

| Step | Detail |
|---|---|
| 1 | Re-fetch each primary vendor product page; diff against the capability set recorded in `00-product/03-market-landscape.md` |
| 2 | Check the most recent Databricks Data + AI Summit and Snowflake Summit announcements — these move the warehouse-native segment fastest |
| 3 | Re-score `00-product/04-competitive-feature-matrix.md`, updating the Atlas column from `60-delivery/04-status-matrix.md` |
| 4 | Re-evaluate the strategic clock in `00-product/05-differentiation-and-whitespace.md` §6 — has anyone shipped governed agent execution for heterogeneous estates? |
| 5 | Update the baseline date in every affected document |

**The one signal that changes the strategy.** If a vendor ships a credible governed-execution plane for a *heterogeneous* estate — not just their own warehouse — the differentiation analysis needs rewriting, not updating. Watch Databricks' Unity AI Gateway and Collibra's AI Command Center most closely.

## Related documents

- Market landscape: `00-product/03-market-landscape.md`
- Competitive feature matrix: `00-product/04-competitive-feature-matrix.md`
- Differentiation and whitespace: `00-product/05-differentiation-and-whitespace.md`
