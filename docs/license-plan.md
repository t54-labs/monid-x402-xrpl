# MIT release license proposal

T54 has approved open-sourcing this gateway. MIT is the concrete license proposed
for this release, consistent with the pinned `x402-xrpl` SDK. The complete text is
prepared in [licenses/MIT.txt](licenses/MIT.txt), using the SDK's existing
`Copyright (c) 2026 t54 Labs` organization spelling. The release coordinator can
apply the following exact change when selecting MIT.

To adopt MIT after confirming that specific license choice:

1. Copy `docs/licenses/MIT.txt` unchanged to root `LICENSE`.
2. Set `project.license = "MIT"` and
   `project.license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]` in pyproject;
   use `setuptools>=77` for this PEP 639 metadata.
3. Include root `LICENSE` in Docker's COPY, both upload allowlists and the source
   manifest. Replace the README proposal link with an MIT license link.
4. Rebuild sdist, wheel and image. Verify root LICENSE, wheel dist-info license
   text, `License-Expression: MIT`, and retained third-party notices. The current
   validation record does not claim these adoption checks have already run.

The root license/metadata is left for the release coordinator's specific license
selection. No visibility change is part of this revision. Third-party licenses,
Monid's hosted-service/data terms and trademark rules remain separate.
