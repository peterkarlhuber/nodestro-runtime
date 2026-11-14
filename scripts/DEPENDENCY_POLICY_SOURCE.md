# Dependency policy K-33 (vendored copy)

`dependency-policy.json` (repository root) and `scripts/check_dependency_policy.py` are unchanged copies from
nodestro-studio, the integrator that holds the edge list K-33 (TABRION AC-2, QA-17). The policy copy holds only
nodestro-runtime's outgoing edges. `python scripts/check_dependency_policy.py` checks this repository's
`pyproject.toml` (core, every extra and dependency group) against it; `tests/test_dependency_policy.py`
checks the copies against the SHA-256 below, runs the checker and probes forbidden requirements.

Origin: `https://github.com/peterkarlhuber/nodestro-studio`, files `scripts/check_dependency_policy.py` and the output
of `python scripts/check_dependency_policy.py --copy nodestro-runtime`, at commit `25095d2d56979c5a1b2bec9c80eecb82b8db18cc`

- `check_dependency_policy.py`: `9c790533d9bc7e34927b48f532dfcb311f6841984430bf501e05304816ff386e`
- `dependency-policy.json`: `5ba65898e9619e6f49b7db7be3e4c1bae4684385c5c438fdb1b88960ed8b0e64`

## Updating the copies

Never edit either file by hand. A new or changed edge goes into nodestro-studio `platform/dependency-policy.json`
first (it needs the plan owner's K-33 decision). Then, in a nodestro-studio checkout, write the two files here:
`python scripts/check_dependency_policy.py --copy nodestro-runtime > <this repo>/dependency-policy.json` and copy
`scripts/check_dependency_policy.py` unchanged. Update the origin commit and both SHA-256 above
(`sha256sum dependency-policy.json scripts/check_dependency_policy.py`) and run the test.
