"""Backward-compatible re-export shim.

Canonical location: `atlas.modules.ingestion.router`, moved under ST-07
Commit C for the ingestion bounded context on 2026-09-03. Every existing
`from aida.ingestion_api import ...` caller keeps working unchanged.

Externally-used symbols at the time of the move:

* `router` -- `aida.main` (mounts it on the app).
* `cancel_metadata_ingestion_batch`, `pause_metadata_ingestion_batch`,
  `resume_metadata_ingestion_batch`, `replay_metadata_ingestion_batch`
  -- `tests/test_in2_batch_controls.py` imports these four handlers
  directly to unit-test batch-control transitions without HTTP.

All other handlers are re-exported here too so a future test that wants
to bypass HTTP for one of them doesn't have to change import paths first.

New code should import from `atlas.modules.ingestion.router` directly.
"""

from atlas.modules.ingestion.router import (
    cancel_metadata_ingestion_batch,
    certify_datasource_connector,
    connector_capability_matrix,
    create_metadata_ingestion_batch,
    finalize_metadata_ingestion_batch,
    get_metadata_ingestion_batch,
    ingest_metadata_envelope,
    list_connector_certifications,
    list_metadata_ingestion_batches,
    list_metadata_ingestion_chunks,
    list_metadata_ingestions,
    pause_metadata_ingestion_batch,
    replay_metadata_ingestion_batch,
    resume_metadata_ingestion_batch,
    router,
    upload_metadata_ingestion_chunk,
)

__all__ = [
    "router",
    "connector_capability_matrix",
    "certify_datasource_connector",
    "list_connector_certifications",
    "ingest_metadata_envelope",
    "list_metadata_ingestions",
    "create_metadata_ingestion_batch",
    "list_metadata_ingestion_batches",
    "get_metadata_ingestion_batch",
    "upload_metadata_ingestion_chunk",
    "list_metadata_ingestion_chunks",
    "finalize_metadata_ingestion_batch",
    "pause_metadata_ingestion_batch",
    "cancel_metadata_ingestion_batch",
    "resume_metadata_ingestion_batch",
    "replay_metadata_ingestion_batch",
]
