# Source provenance and authorization status

This records engineering evidence, not a declaration of ownership or permission.
T54 must confirm contributor rights and the scope of any copied/adapted source
before selecting the gateway's license. No project license is granted by this PR.

## T54 reference projects

The gateway follows T54's existing FastAPI / x402 XRPL integration patterns. The
following internal projects were identified as references and are acknowledged
here; their authorization must be resolved before a public release:

| Reference | Snapshot inspected | Evidence and unresolved work |
| --- | --- | --- |
| [xrpl-x402-inference-provider](https://github.com/t54-labs/xrpl-x402-inference-provider) | `81b4e076463e69bb031c39d0c5feed1849e8151a` (local historical snapshot); remote main `2bcbed7578aadc5ebff86cc0de3d1cf9c9cac9ea` | No tracked LICENSE/NOTICE or project license declaration on remote main. Short exact blocks in gateway config/payment match issuer constants, settings options and imports. T54 must confirm actual reuse, authorship and permission to relicense any adapted code. |
| [ask-surf-x402](https://github.com/t54-labs/ask-surf-x402) | `88b6568a1d745430be29ae47b5f4d96744455b9f` | README and pyproject declare MIT, but no root LICENSE/copyright grant is tracked. A nested `libs/x402python/LICENSE` covers that separate library, not automatically the full template. A short import block matches the initial gateway. Verify the actual copyright holder and complete notice if source was copied. |

Method: compare tracked Python source in the gateway's initial commit
`eaa3c96fe986efd767a61e98800eb0b76430ccab` and assessed main
`6221d9241b0433fa881afb296a2c3cf59319fb0f` against the snapshots above using exact
contiguous line matching (at least five lines, at least four nonempty).
For the initial gateway, the longest config match to the inference reference was
11 lines and consisted of settings boilerplate. Payment matches were imports.
The ask-surf match was a seven-line import block. These common constructs show
similarity but do not establish copyright derivation, independent authorship or
the complete development history. The absence of a longer match is not proof
that no code or design was adapted. Maintainer confirmation remains required.

## SDK and upstream service

`x402-xrpl==0.3.4` is a dependency, not vendored source. Its installed distribution
includes an MIT license with copyright `(c) 2026 t54 Labs`. `xrpl-py==5.2.0` carries
an ISC notice for the XRP Ledger Foundation. Preserve their upstream notices when
redistributing those packages; see [third-party notices](../THIRD_PARTY_NOTICES.md).

This gateway calls Monid's HTTP API. It does not vendor Monid connectors, logo
artwork or catalog/result snapshots. The [Monid connector repository](https://github.com/monid-ai/monid)
declares MIT for that repository's code; that is not a blanket grant for hosted API
access, supplier data or trademarks.

[Monid's proxy documentation](https://monid.ai/docs/integrations/proxy) explicitly
describes forwarding calls with the operator's credentials and charging users with
or without markup. T54 must still review its actual account agreement, applicable
provider/data restrictions, result retention, pricing and paid-service terms.
[Monid brand guidelines](https://monid.ai/brand/) allow referential integration use
and require approval for co-branding/partner material. The configured merchant
name, favicon and provider-logo URLs are not relicensed by this gateway. Confirm
the intended presentation or choose an independent brand before launch.

No external party was contacted as part of this preparation.
