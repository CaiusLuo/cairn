# Repository intelligence v2 contract

This is the corrected deterministic-check contract for the optional repository
intelligence experiment tracked by [#28](https://github.com/CaiusLuo/cairn/issues/28).
It keeps the six v1 cases, prompts, repository contents, memory fixtures and
reference behaviors unchanged.

The versioned dataset is implemented in
`src/cairn/runtime/evals/intelligence/dataset.py`. The current contract is
`repo-intelligence-v2` with SHA256
`12dfcc1d70c28cfd3a670b8bfe85a0f3d766c370416d75f3072bbe11e2426cdf`.
Unlike v1, `checks/check_account.py` is protected by a preservation check for
every case except `testing-convention`, where that file is an allowed target.

The historical `repo-intelligence-v1` contract remains explicitly available
with SHA256
`b459ce3fd1293e540572a2ebf8ae0c4a10407fd28a4be76f117a776d45e7553f`.
Its 13-run pilot artifacts and conclusions under
`docs/evals/repository-intelligence-v1/pilot-20261011` are frozen and must not be
reinterpreted using the v2 oracle.

Graph discovery now counts a returned dependency target only when the graph
record is resolved and has a non-null `target_path`. It does not infer paths
from unresolved, ambiguous or unsupported relationships. Duplicate evidence is
still counted once per path.

No paid provider pilot was run for this correction. Any future provider run
must use a new output directory and retain its explicit dataset version and
hash.
