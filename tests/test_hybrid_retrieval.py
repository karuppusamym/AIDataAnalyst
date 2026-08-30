"""Comprehensive tests for the hybrid retrieval engine (RT-1 through RT-9).

Pure unit tests -- no database required.  Covers:
  - Fusion ranking logic (RRF and weighted linear)
  - Graph expansion logic (bounded BFS, org scoping)
  - Full-text query generation
  - Vector similarity search
  - Cross-source search scoping
  - Evidence completeness
  - Policy filtering before ranking (leak test)
"""

from uuid import UUID, uuid4

import pytest

from aida.full_text_index import (
    FullTextHit,
    build_search_document,
    build_ts_query,
    full_text_rank,
)
from aida.fusion_ranking import (
    FactorDetail,
    FusionConfig,
    RankedCandidate,
    SignalScore,
    build_evidence,
    fuse_results,
    merge_candidates,
    reciprocal_rank_fusion,
    weighted_linear_fusion,
)
from aida.graph_retrieval import (
    GraphEdge,
    GraphHit,
    GraphNode,
    KnowledgeGraph,
    expand_graph,
)
from aida.vector_retrieval import (
    HashEmbeddingProvider,
    VectorHit,
    build_embedding_text,
    cosine_similarity,
    vector_search,
)


# =====================================================================
# Full-text index tests (RT-4)
# =====================================================================


class TestBuildTsQuery:
    def test_strips_stop_words_and_joins_with_conjunction(self) -> None:
        result = build_ts_query("Show me the customer revenue")
        assert result == "customer & revenue"

    def test_splits_snake_case_and_camel_case(self) -> None:
        result = build_ts_query("total_revenue NetIncome")
        assert "total" in result
        assert "revenue" in result
        assert "net" in result
        assert "income" in result

    def test_empty_string_returns_empty(self) -> None:
        assert build_ts_query("") == ""

    def test_all_stop_words_returns_empty(self) -> None:
        assert build_ts_query("the a an is are") == ""

    def test_or_conjunction(self) -> None:
        result = build_ts_query("customer revenue", conjunction="|")
        assert result == "customer | revenue"

    def test_deduplicates_tokens(self) -> None:
        result = build_ts_query("revenue revenue report")
        tokens = result.replace(" & ", " ").split()
        assert tokens == ["revenue", "report"]


class TestBuildSearchDocument:
    def test_includes_name_and_description(self) -> None:
        doc = build_search_document(
            name="dim_customer",
            description="Customer dimension table",
        )
        assert "dim_customer" in doc
        assert "Customer dimension table" in doc

    def test_includes_synonyms_and_tags(self) -> None:
        doc = build_search_document(
            name="orders",
            synonyms=["purchases", "transactions"],
            tags=["finance", "core"],
        )
        assert "purchases" in doc
        assert "finance" in doc


class TestFullTextRank:
    def test_ranks_documents_by_relevance(self) -> None:
        docs = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "dim_region",
                "text": "Region reference dimension",
            },
            {
                "object_type": "TABLE",
                "object_id": "2",
                "display_name": "fact_revenue",
                "text": "Net revenue after returns and discounts",
            },
        ]
        hits = full_text_rank("net revenue", docs)
        assert len(hits) >= 1
        # Revenue table should rank first
        assert hits[0].object_id == "2"

    def test_excludes_zero_score_hits(self) -> None:
        docs = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "xyz",
                "text": "completely unrelated content about xyz",
            },
        ]
        hits = full_text_rank("customer revenue", docs)
        # If no query token appears, score is 0 and hit is excluded
        for hit in hits:
            assert hit.ts_rank > 0

    def test_empty_query_returns_empty(self) -> None:
        docs = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "test",
                "text": "test data",
            },
        ]
        hits = full_text_rank("the a an", docs)
        assert hits == []


# =====================================================================
# Vector retrieval tests (RT-1)
# =====================================================================


class TestHashEmbeddingProvider:
    def test_produces_correct_dimension(self) -> None:
        provider = HashEmbeddingProvider(dimension=32)
        emb = provider.embed("test")
        assert len(emb) == 32

    def test_deterministic_output(self) -> None:
        provider = HashEmbeddingProvider()
        e1 = provider.embed("customer revenue")
        e2 = provider.embed("customer revenue")
        assert e1 == e2

    def test_different_inputs_produce_different_embeddings(self) -> None:
        provider = HashEmbeddingProvider()
        e1 = provider.embed("customer")
        e2 = provider.embed("revenue")
        assert e1 != e2

    def test_normalised_to_unit_length(self) -> None:
        import math

        provider = HashEmbeddingProvider()
        emb = provider.embed("test embedding")
        norm = math.sqrt(sum(v * v for v in emb))
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_model_name(self) -> None:
        provider = HashEmbeddingProvider()
        assert provider.model_name == "hash-deterministic-v1"


class TestCosineSimilarity:
    def test_identical_vectors_return_one(self) -> None:
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_return_negative_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors_return_zero(self) -> None:
        assert cosine_similarity([], []) == 0.0

    def test_different_lengths_return_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0


class TestVectorSearch:
    def test_returns_sorted_by_similarity(self) -> None:
        provider = HashEmbeddingProvider()
        q_emb = provider.embed("customer revenue")
        candidates = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "unrelated_xyz",
                "embedding": provider.embed("unrelated xyz data"),
            },
            {
                "object_type": "TABLE",
                "object_id": "2",
                "display_name": "customer_revenue",
                "embedding": provider.embed("customer revenue"),
            },
        ]
        # Use min_similarity=-1.0 so even low/negative similarities are included
        hits = vector_search(q_emb, candidates, top_k=10, min_similarity=-1.0)
        assert len(hits) == 2
        # Exact match should be first
        assert hits[0].object_id == "2"
        assert hits[0].similarity == pytest.approx(1.0, abs=1e-4)

    def test_respects_top_k(self) -> None:
        provider = HashEmbeddingProvider()
        q_emb = provider.embed("test")
        candidates = [
            {
                "object_type": "TABLE",
                "object_id": str(i),
                "display_name": f"table_{i}",
                "embedding": provider.embed(f"table {i}"),
            }
            for i in range(20)
        ]
        hits = vector_search(q_emb, candidates, top_k=5)
        assert len(hits) == 5

    def test_respects_min_similarity(self) -> None:
        provider = HashEmbeddingProvider()
        q_emb = provider.embed("customer")
        candidates = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "customer",
                "embedding": provider.embed("customer"),
            },
            {
                "object_type": "TABLE",
                "object_id": "2",
                "display_name": "xyz",
                "embedding": provider.embed("completely different xyz abc 123"),
            },
        ]
        hits = vector_search(q_emb, candidates, min_similarity=0.9)
        # Only the near-exact match should survive
        assert all(h.similarity >= 0.9 for h in hits)


class TestBuildEmbeddingText:
    def test_includes_all_fields(self) -> None:
        text = build_embedding_text(
            name="dim_customer",
            description="Customer dimension",
            object_type="TABLE",
            synonyms=["clients"],
            tags=["core"],
        )
        assert "TABLE" in text
        assert "dim_customer" in text
        assert "Customer dimension" in text
        assert "clients" in text
        assert "core" in text


# =====================================================================
# Graph retrieval tests (RT-2)
# =====================================================================


class TestKnowledgeGraph:
    def test_add_and_retrieve_nodes(self) -> None:
        kg = KnowledgeGraph()
        org = uuid4()
        node = GraphNode(
            node_id="T:1", node_type="TABLE",
            display_name="orders", organization_id=org,
        )
        kg.add_node(node)
        assert kg.get_node("T:1") == node
        assert kg.node_count == 1

    def test_add_and_retrieve_edges(self) -> None:
        kg = KnowledgeGraph()
        org = uuid4()
        kg.add_node(GraphNode(
            node_id="T:1", node_type="TABLE",
            display_name="orders", organization_id=org,
        ))
        kg.add_node(GraphNode(
            node_id="T:2", node_type="TABLE",
            display_name="customers", organization_id=org,
        ))
        kg.add_edge(GraphEdge(
            source_id="T:1", target_id="T:2",
            edge_type="FOREIGN_KEY",
        ))
        edges = kg.get_edges("T:1")
        assert len(edges) == 1
        assert edges[0].target_id == "T:2"
        # Reverse edge should also exist
        rev_edges = kg.get_edges("T:2")
        assert len(rev_edges) == 1
        assert rev_edges[0].target_id == "T:1"


class TestExpandGraph:
    def _build_chain(
        self, org_id: UUID, length: int = 5
    ) -> tuple[KnowledgeGraph, list[str]]:
        """Build a linear chain graph: T:0 -> T:1 -> ... -> T:n."""
        kg = KnowledgeGraph()
        ids = [f"T:{i}" for i in range(length)]
        for i, nid in enumerate(ids):
            kg.add_node(GraphNode(
                node_id=nid, node_type="TABLE",
                display_name=f"table_{i}", organization_id=org_id,
            ))
        for i in range(length - 1):
            kg.add_edge(GraphEdge(
                source_id=ids[i], target_id=ids[i + 1],
                edge_type="FOREIGN_KEY",
            ))
        return kg, ids

    def test_bounded_hop_expansion(self) -> None:
        org = uuid4()
        kg, ids = self._build_chain(org, length=6)
        hits = expand_graph(
            kg, [ids[0]], allowed_org_id=org, max_hops=2,
        )
        # From T:0, max_hops=2 should reach T:1 (depth 1) and T:2 (depth 2)
        hit_ids = {h.object_id for h in hits}
        assert "T:1" in hit_ids
        assert "T:2" in hit_ids
        # T:3 and beyond are unreachable with 2 hops
        assert "T:3" not in hit_ids

    def test_expansion_path_is_recorded(self) -> None:
        org = uuid4()
        kg, ids = self._build_chain(org, length=4)
        hits = expand_graph(
            kg, [ids[0]], allowed_org_id=org, max_hops=3,
        )
        # The deepest hit should have the full expansion path
        deep_hits = [h for h in hits if h.depth == 3]
        assert len(deep_hits) == 1
        assert deep_hits[0].expansion_path == ["T:0", "T:1", "T:2", "T:3"]

    def test_org_boundary_is_respected(self) -> None:
        org_a = uuid4()
        org_b = uuid4()
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(
            node_id="T:1", node_type="TABLE",
            display_name="table_1", organization_id=org_a,
        ))
        kg.add_node(GraphNode(
            node_id="T:2", node_type="TABLE",
            display_name="table_2", organization_id=org_b,
        ))
        kg.add_edge(GraphEdge(
            source_id="T:1", target_id="T:2",
            edge_type="FOREIGN_KEY",
        ))
        hits = expand_graph(
            kg, ["T:1"], allowed_org_id=org_a, max_hops=2,
        )
        # T:2 belongs to org_b so should NOT be reachable
        hit_ids = {h.object_id for h in hits}
        assert "T:2" not in hit_ids

    def test_seed_in_wrong_org_is_skipped(self) -> None:
        org_a = uuid4()
        org_b = uuid4()
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(
            node_id="T:1", node_type="TABLE",
            display_name="table_1", organization_id=org_b,
        ))
        hits = expand_graph(
            kg, ["T:1"], allowed_org_id=org_a, max_hops=2,
        )
        assert hits == []

    def test_max_results_cap(self) -> None:
        org = uuid4()
        kg, ids = self._build_chain(org, length=20)
        hits = expand_graph(
            kg, [ids[0]], allowed_org_id=org, max_hops=20, max_results=3,
        )
        assert len(hits) <= 3

    def test_proximity_score_decays_with_depth(self) -> None:
        org = uuid4()
        kg, ids = self._build_chain(org, length=4)
        hits = expand_graph(
            kg, [ids[0]], allowed_org_id=org, max_hops=3,
        )
        scores_by_depth = {h.depth: h.proximity_score for h in hits}
        # depth 1: 1/(1+1) = 0.5, depth 2: 1/(1+2) ~= 0.3333
        assert scores_by_depth[1] > scores_by_depth[2]
        if 3 in scores_by_depth:
            assert scores_by_depth[2] > scores_by_depth[3]

    def test_nonexistent_seed_is_silently_skipped(self) -> None:
        org = uuid4()
        kg = KnowledgeGraph()
        hits = expand_graph(
            kg, ["NONEXISTENT"], allowed_org_id=org, max_hops=2,
        )
        assert hits == []


# =====================================================================
# Fusion ranking tests (RT-3)
# =====================================================================


def _candidate(
    obj_id: str, signals: dict[str, float], display_name: str = "test"
) -> RankedCandidate:
    return RankedCandidate(
        object_type="TABLE",
        object_id=obj_id,
        display_name=display_name,
        signals=[
            SignalScore(signal=name, raw_score=score)
            for name, score in signals.items()
        ],
    )


class TestReciprocalRankFusion:
    def test_rrf_ranks_multi_signal_candidate_higher(self) -> None:
        config = FusionConfig(method="rrf")
        c1 = _candidate("1", {"lexical": 0.9, "vector": 0.8})
        c2 = _candidate("2", {"lexical": 0.95})
        ranked = fuse_results([c1, c2], config=config)
        # c1 has signals from two sources so should generally rank higher
        assert ranked[0].object_id == "1"

    def test_rrf_assigns_nonzero_scores(self) -> None:
        config = FusionConfig(method="rrf")
        c = _candidate("1", {"lexical": 0.5})
        ranked = fuse_results([c], config=config)
        assert ranked[0].final_score > 0

    def test_rrf_preserves_all_candidates(self) -> None:
        config = FusionConfig(method="rrf")
        candidates = [
            _candidate(str(i), {"lexical": 0.5 - i * 0.01})
            for i in range(10)
        ]
        ranked = fuse_results(candidates, config=config, top_k=100)
        assert len(ranked) == 10


class TestWeightedLinearFusion:
    def test_weighted_linear_respects_weights(self) -> None:
        config = FusionConfig(
            method="weighted_linear",
            lexical_weight=0.8,
            vector_weight=0.2,
            graph_weight=0.0,
            quality_trust_weight=0.0,
            usage_popularity_weight=0.0,
        )
        c1 = _candidate("1", {"lexical": 0.9, "vector": 0.1})
        c2 = _candidate("2", {"lexical": 0.1, "vector": 0.9})
        ranked = fuse_results([c1, c2], config=config)
        # c1 should win because lexical has higher weight
        assert ranked[0].object_id == "1"

    def test_weighted_linear_score_computation(self) -> None:
        config = FusionConfig(
            method="weighted_linear",
            lexical_weight=0.5,
            vector_weight=0.5,
            graph_weight=0.0,
            quality_trust_weight=0.0,
            usage_popularity_weight=0.0,
        )
        c = _candidate("1", {"lexical": 0.8, "vector": 0.6})
        ranked = fuse_results([c], config=config)
        expected = 0.5 * 0.8 + 0.5 * 0.6
        assert ranked[0].final_score == pytest.approx(expected, abs=1e-6)


class TestFusionTopK:
    def test_fuse_results_respects_top_k(self) -> None:
        candidates = [_candidate(str(i), {"lexical": 1.0 - i * 0.01}) for i in range(20)]
        ranked = fuse_results(candidates, top_k=5)
        assert len(ranked) == 5

    def test_empty_input_returns_empty(self) -> None:
        ranked = fuse_results([])
        assert ranked == []


class TestBuildEvidence:
    def test_evidence_has_all_factors(self) -> None:
        config = FusionConfig(method="rrf")
        c = _candidate("1", {"lexical": 0.8, "vector": 0.6, "graph": 0.4})
        fuse_results([c], config=config)
        evidence = build_evidence(c, config)
        signal_names = {f.signal for f in evidence}
        assert "lexical" in signal_names
        assert "vector" in signal_names
        assert "graph" in signal_names

    def test_evidence_includes_rank_and_weight(self) -> None:
        config = FusionConfig(method="rrf")
        c = _candidate("1", {"lexical": 0.8})
        fuse_results([c], config=config)
        evidence = build_evidence(c, config)
        lexical_factor = next(f for f in evidence if f.signal == "lexical")
        assert lexical_factor.rank is not None
        assert lexical_factor.weight > 0
        assert lexical_factor.weighted_score > 0


class TestMergeCandidates:
    def test_merges_same_object_from_different_signals(self) -> None:
        lexical_results = [
            ("TABLE", "1", "orders", 0.9, {}),
            ("TABLE", "2", "customers", 0.7, {}),
        ]
        vector_results = [
            ("TABLE", "1", "orders", 0.8, {}),
            ("TABLE", "3", "products", 0.6, {}),
        ]
        merged = merge_candidates(
            lexical_results, vector_results,
            signal_names=["lexical", "vector"],
        )
        # 3 unique objects total
        assert len(merged) == 3
        # Object "1" should have both signals
        obj1 = next(c for c in merged if c.object_id == "1")
        signal_names = [s.signal for s in obj1.signals]
        assert "lexical" in signal_names
        assert "vector" in signal_names


# =====================================================================
# Cross-source search scoping tests
# =====================================================================


class TestCrossSourceScoping:
    """Verify that search results respect organization boundaries."""

    def test_full_text_rank_does_not_filter_by_org(self) -> None:
        """full_text_rank operates on pre-filtered documents --
        the caller is responsible for org-scoping BEFORE calling."""
        docs = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "orders",
                "text": "customer orders",
                "datasource_id": str(uuid4()),
            },
        ]
        hits = full_text_rank("orders", docs)
        assert len(hits) == 1

    def test_vector_search_operates_on_pre_filtered_candidates(self) -> None:
        """vector_search expects callers to filter BEFORE ranking."""
        provider = HashEmbeddingProvider()
        q_emb = provider.embed("test")
        # Only pass candidates that have already been org-filtered
        candidates = [
            {
                "object_type": "TABLE",
                "object_id": "1",
                "display_name": "allowed",
                "embedding": provider.embed("test"),
            },
        ]
        hits = vector_search(q_emb, candidates)
        assert len(hits) == 1

    def test_graph_expansion_enforces_org_boundary(self) -> None:
        """expand_graph filters by allowed_org_id."""
        org_a = uuid4()
        org_b = uuid4()
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(
            node_id="T:1", node_type="TABLE",
            display_name="a", organization_id=org_a,
        ))
        kg.add_node(GraphNode(
            node_id="T:2", node_type="TABLE",
            display_name="b", organization_id=org_b,
        ))
        kg.add_edge(GraphEdge(
            source_id="T:1", target_id="T:2",
            edge_type="FK",
        ))
        hits = expand_graph(kg, ["T:1"], allowed_org_id=org_a, max_hops=2)
        assert all(h.object_id != "T:2" for h in hits)


# =====================================================================
# Policy filtering BEFORE ranking (leak test)
# =====================================================================


class TestPolicyFilterBeforeRanking:
    """The core invariant: policy filtering happens BEFORE ranking,
    not after.  This test proves that a high-scoring result from a
    different org is never visible."""

    def test_graph_expansion_never_leaks_cross_org_nodes(self) -> None:
        org_allowed = uuid4()
        org_forbidden = uuid4()
        kg = KnowledgeGraph()
        # Allowed org node
        kg.add_node(GraphNode(
            node_id="SEED", node_type="TABLE",
            display_name="seed", organization_id=org_allowed,
        ))
        # Forbidden org node (connected via edge)
        kg.add_node(GraphNode(
            node_id="LEAKED", node_type="TABLE",
            display_name="leaked_secret_table", organization_id=org_forbidden,
        ))
        kg.add_edge(GraphEdge(
            source_id="SEED", target_id="LEAKED",
            edge_type="FK",
        ))
        hits = expand_graph(
            kg, ["SEED"], allowed_org_id=org_allowed, max_hops=10,
        )
        # The forbidden node must NEVER appear in results
        leaked_ids = {h.object_id for h in hits}
        assert "LEAKED" not in leaked_ids

    def test_vector_search_operates_only_on_pre_filtered_set(self) -> None:
        """Demonstrate that vector_search has no ability to include
        candidates not passed to it -- the caller must filter first."""
        provider = HashEmbeddingProvider()
        q_emb = provider.embed("secret table")
        # Only org-allowed candidates are passed
        org_allowed_candidates = [
            {
                "object_type": "TABLE",
                "object_id": "SAFE",
                "display_name": "safe",
                "embedding": provider.embed("safe table"),
            },
        ]
        # The forbidden candidate is never given to vector_search
        hits = vector_search(q_emb, org_allowed_candidates)
        ids = {h.object_id for h in hits}
        assert "LEAKED" not in ids

    def test_fusion_ranking_cannot_introduce_unfiltered_candidates(self) -> None:
        """Fusion ranking operates on the candidate list it receives;
        it cannot conjure new results."""
        config = FusionConfig()
        # Only safe candidates in the input
        candidates = [_candidate("SAFE", {"lexical": 0.9})]
        ranked = fuse_results(candidates, config=config)
        assert all(c.object_id == "SAFE" for c in ranked)


# =====================================================================
# Evidence completeness tests
# =====================================================================


class TestEvidenceCompleteness:
    def test_every_factor_has_required_fields(self) -> None:
        config = FusionConfig(method="weighted_linear")
        c = _candidate(
            "1",
            {
                "lexical": 0.8,
                "vector": 0.7,
                "graph": 0.3,
                "quality_trust": 0.5,
                "usage_popularity": 0.4,
            },
        )
        fuse_results([c], config=config)
        evidence = build_evidence(c, config)
        for factor in evidence:
            assert isinstance(factor.signal, str)
            assert isinstance(factor.raw_score, float)
            assert isinstance(factor.weight, float)
            assert isinstance(factor.weighted_score, float)

    def test_evidence_covers_all_signals(self) -> None:
        config = FusionConfig(method="rrf")
        c = _candidate(
            "1",
            {
                "lexical": 0.8,
                "vector": 0.7,
                "graph": 0.3,
                "quality_trust": 0.5,
                "usage_popularity": 0.4,
            },
        )
        fuse_results([c], config=config)
        evidence = build_evidence(c, config)
        evidence_signals = {f.signal for f in evidence}
        assert evidence_signals == {
            "lexical", "vector", "graph", "quality_trust", "usage_popularity"
        }

    def test_weighted_scores_sum_to_final_score_for_linear(self) -> None:
        config = FusionConfig(method="weighted_linear")
        c = _candidate("1", {"lexical": 0.8, "vector": 0.6})
        fuse_results([c], config=config)
        evidence = build_evidence(c, config)
        total = sum(f.weighted_score for f in evidence)
        assert total == pytest.approx(c.final_score, abs=1e-6)

    def test_rrf_evidence_includes_ranks(self) -> None:
        config = FusionConfig(method="rrf")
        c1 = _candidate("1", {"lexical": 0.9, "vector": 0.8})
        c2 = _candidate("2", {"lexical": 0.7, "vector": 0.6})
        fuse_results([c1, c2], config=config)
        evidence = build_evidence(c1, config)
        for factor in evidence:
            assert factor.rank is not None
            assert factor.rank >= 1


# =====================================================================
# Quality trust and usage popularity placeholders
# =====================================================================


class TestPlaceholderSignals:
    def test_quality_trust_placeholder_contributes_to_score(self) -> None:
        config = FusionConfig(
            method="weighted_linear",
            lexical_weight=0.0,
            vector_weight=0.0,
            graph_weight=0.0,
            quality_trust_weight=1.0,
            usage_popularity_weight=0.0,
        )
        c = _candidate("1", {"quality_trust": 0.8})
        ranked = fuse_results([c], config=config)
        assert ranked[0].final_score == pytest.approx(0.8, abs=1e-6)

    def test_usage_popularity_placeholder_contributes_to_score(self) -> None:
        config = FusionConfig(
            method="weighted_linear",
            lexical_weight=0.0,
            vector_weight=0.0,
            graph_weight=0.0,
            quality_trust_weight=0.0,
            usage_popularity_weight=1.0,
        )
        c = _candidate("1", {"usage_popularity": 0.7})
        ranked = fuse_results([c], config=config)
        assert ranked[0].final_score == pytest.approx(0.7, abs=1e-6)


# =====================================================================
# Integration: fusion method selection
# =====================================================================


class TestFusionMethodSelection:
    def test_invalid_method_raises(self) -> None:
        config = FusionConfig(method="invalid")
        with pytest.raises(ValueError, match="Unknown fusion method"):
            fuse_results([_candidate("1", {"lexical": 0.5})], config=config)

    def test_rrf_method_works(self) -> None:
        config = FusionConfig(method="rrf")
        ranked = fuse_results([_candidate("1", {"lexical": 0.5})], config=config)
        assert len(ranked) == 1

    def test_weighted_linear_method_works(self) -> None:
        config = FusionConfig(method="weighted_linear")
        ranked = fuse_results([_candidate("1", {"lexical": 0.5})], config=config)
        assert len(ranked) == 1
