Pre-run review gates for broad validation (set before live scores):
- Freeze full suite and checksums before inference; retain all 30 outcomes, including failures.
- No missing cases, transport/model exceptions, citation/provenance violations, filter leakage, retry/context/concurrency bound violations, or fabricated claims.
- All unavailable scopes publish zero claims; mixed cases preserve the unavailable scope and produce supported available-scope evidence.
- At least 80% of supported planned subqueries have independently verified published citation coverage. Report all partials/flags; this is a structural coverage threshold, not semantic accuracy.
- Complete Python suite and applicable Go race/vet checks pass.
- Public snapshot contains no local model weights, datasets, credentials or private contact information.
Push only after these gates pass; otherwise fix reproducible issues through Claude Code, preserve attempts, and run a fresh versioned evaluation without deleting failures or cherry-picking cases.
