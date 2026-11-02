"""Every explicit bound of the audit workbench in one place (mirrored in docs/build/audit-ui.md)."""
MAX_PROMPT_BYTES = 8192           # UTF-8 bytes; identical to the agent's own limit
MAX_BODY_BYTES = 16384            # any request body, enforced while streaming (Content-Length or chunked)
MAX_HISTORY = 8                   # finished (completed/failed/cancelled) jobs retained in memory
HISTORY_TTL_SECONDS = 1800        # finished jobs expire 30 minutes after they finish
MAX_RESULT_BYTES = 1 << 20        # serialized result JSON per job; larger results fail explicitly
JOB_TIMEOUT_MAX_SECONDS = 600     # hard ceiling for the configurable per-job timeout
MODEL_TIMEOUT_MAX_SECONDS = 180   # hard ceiling for one structured model call
MODEL_CONCURRENCY = 2             # concurrent Claude CLI calls (ClaudeJSON default, unchanged)
MAX_CATALOG_FILINGS = 10000       # registry catalog size bound (ProvenanceRegistry already enforces 1..10000)
MAX_MODEL_CALL_ROWS = 64          # diagnostic rows kept per result
UVICORN_LIMIT_CONCURRENCY = 32    # simultaneous HTTP connections/tasks accepted by uvicorn
UVICORN_BACKLOG = 64
MAX_HEADER_BYTES = 16384          # h11 incomplete-event (header block) limit
RETRY_AFTER_SECONDS = 5
SHUTDOWN_WARNING_SECONDS = 45     # shutdown logs a warning every interval while cleanup is pending; it never abandons the wait
