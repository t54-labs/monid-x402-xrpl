# Third-party notices and dependency inventory

This file acknowledges dependencies and reference projects. T54 has authorized
open-sourcing this gateway. The concrete MIT license proposal is in
[the license plan](docs/license-plan.md); source attribution is recorded in
[provenance](docs/provenance.md). Third-party components retain their own licenses.

## References and supplier material

T54's `xrpl-x402-inference-provider` and `ask-surf-x402` projects informed the
integration review. Their exact snapshots and comparison evidence are recorded in
`docs/provenance.md`. The inference reference has no declared license; the ask-surf
reference declares MIT without a root license text. The gateway's release does not
change those repositories' licenses or replace notices for separate third-party
libraries included there.

Monid service access, catalog/result data, its name/logo, and provider logos are
subject to their own rights and terms. Referencing their URLs does not relicense
those materials. This source tree does not vendor the upstream catalog, results,
logos or SDK implementations.

## Distribution obligations

The table below is a dated metadata inventory, not replacement license text or an
assertion that every locked optional/platform package is present in every image.
Dependencies are installed separately from their upstream distributions. Preserve
their complete license/copyright and NOTICE files when distributing a package,
container or bundled application. The Docker build retains dependency metadata;
verify the actual image and target-platform bundled libraries before publication.

- `x402-xrpl` 0.3.4 includes an MIT license, copyright (c) 2026 t54 Labs.
- `xrpl-py` 5.2.0 includes an ISC notice, copyright (c) 2021 XRP Ledger Foundation.
- MIT/BSD/ISC components require retention of their applicable notices. Apache
  components require their license and applicable NOTICE/attribution handling.
- `certifi` is MPL-2.0; `tqdm` declares MPL-2.0 AND MIT. Review file-level notice,
  source availability and modification obligations for the files redistributed.
- `psycopg` and `psycopg-binary` declare LGPL-3.0-only. Review the shipped library,
  replacement/relinking and corresponding-source obligations for the distribution
  format and any bundled native dependencies. These declarations do not alone
  require relicensing all gateway code under LGPL.

## Locked dependency inventory (2026-10-19)

Source: `uv.lock` at assessed base `6221d9241b0433fa881afb296a2c3cf59319fb0f`.
65 matching installed distributions were inspected read-only (METADATA and selected
license texts); the three platform/conditional entries absent locally were checked
against their exact PyPI release pages. The build-system requirement `setuptools>=69`
is not pinned in this lock and is outside these 68 rows. Docker base images,
OS packages, uv and bundled native-library subcomponents need a separate inventory
for an actual distributable image. No complete license compatibility opinion or
supply-chain/vulnerability audit is implied.

| Package | Locked version | Declared license / inspected evidence |
| --- | --- | --- |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | MIT |
| [async-timeout](https://pypi.org/project/async-timeout/5.0.1/) | 5.0.1 | Apache-2.0; PyPI metadata |
| [base58](https://pypi.org/project/base58/2.1.1/) | 2.1.1 | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/3.5.2/) | 3.5.2 | MIT |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | BSD-3-Clause |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | BSD-3-Clause; PyPI metadata |
| [cryptography](https://pypi.org/project/cryptography/50.0.2/) | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| [deprecated](https://pypi.org/project/deprecated/1.3.1/) | 1.3.1 | MIT |
| [distro](https://pypi.org/project/distro/1.9.0/) | 1.9.0 | Apache License, Version 2.0 |
| [ecpy](https://pypi.org/project/ecpy/1.2.5/) | 1.2.5 | Apache License - Version 2.0 |
| [fastapi](https://pypi.org/project/fastapi/0.115.14/) | 0.115.14 | MIT License |
| [google-api-core](https://pypi.org/project/google-api-core/2.42.0/) | 2.42.0 | Apache 2.0 |
| [google-auth](https://pypi.org/project/google-auth/2.62.0/) | 2.62.0 | Apache 2.0 |
| [google-cloud-core](https://pypi.org/project/google-cloud-core/2.8.0/) | 2.8.0 | Apache 2.0 |
| [google-cloud-storage](https://pypi.org/project/google-cloud-storage/2.19.0/) | 2.19.0 | Apache 2.0 |
| [google-crc32c](https://pypi.org/project/google-crc32c/1.9.0/) | 1.9.0 | Apache-2.0 |
| [google-resumable-media](https://pypi.org/project/google-resumable-media/2.11.0/) | 2.11.0 | Apache 2.0 |
| [googleapis-common-protos](https://pypi.org/project/googleapis-common-protos/1.75.5/) | 1.75.5 | Apache-2.0 |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| [httptools](https://pypi.org/project/httptools/0.9.0/) | 0.9.0 | MIT |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.1/) | 2.3.1 | MIT |
| [jiter](https://pypi.org/project/jiter/0.17.0/) | 0.17.0 | MIT |
| [openai](https://pypi.org/project/openai/1.109.1/) | 1.109.1 | Apache-2.0 |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.45.1/) | 1.45.1 | Apache-2.0 |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pluggy](https://pypi.org/project/pluggy/1.7.0/) | 1.7.0 | MIT |
| [prometheus-client](https://pypi.org/project/prometheus-client/0.21.1/) | 0.21.1 | Apache Software License 2.0 |
| [proto-plus](https://pypi.org/project/proto-plus/1.29.0/) | 1.29.0 | Apache 2.0 |
| [protobuf](https://pypi.org/project/protobuf/7.36.2/) | 7.36.2 | 3-Clause BSD License |
| [psycopg](https://pypi.org/project/psycopg/3.3.6/) | 3.3.6 | LGPL-3.0-only |
| [psycopg-binary](https://pypi.org/project/psycopg-binary/3.3.6/) | 3.3.6 | LGPL-3.0-only |
| [pyasn1](https://pypi.org/project/pyasn1/0.6.4/) | 0.6.4 | BSD-2-Clause |
| [pyasn1-modules](https://pypi.org/project/pyasn1-modules/0.4.2/) | 0.4.2 | BSD |
| [pycparser](https://pypi.org/project/pycparser/3.11/) | 3.11 | BSD-3-Clause |
| [pycryptodome](https://pypi.org/project/pycryptodome/3.24.1/) | 3.24.1 | BSD, Public Domain |
| [pydantic](https://pypi.org/project/pydantic/2.14.1/) | 2.14.1 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.50.1/) | 2.50.1 | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/2.16.0/) | 2.16.0 | MIT |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [pyjwt](https://pypi.org/project/pyjwt/2.15.1/) | 2.15.1 | MIT |
| [pytest](https://pypi.org/project/pytest/8.4.2/) | 8.4.2 | MIT |
| [pytest-asyncio](https://pypi.org/project/pytest-asyncio/0.26.0/) | 0.26.0 | Apache-2.0 |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/) | 1.2.4 | BSD-3-Clause |
| [pyyaml](https://pypi.org/project/pyyaml/6.0.3/) | 6.0.3 | MIT |
| [redis](https://pypi.org/project/redis/5.3.1/) | 5.3.1 | MIT |
| [requests](https://pypi.org/project/requests/2.34.2/) | 2.34.2 | Apache-2.0 |
| [ruff](https://pypi.org/project/ruff/0.17.1/) | 0.17.1 | MIT |
| [sniffio](https://pypi.org/project/sniffio/1.3.1/) | 1.3.1 | MIT OR Apache-2.0 |
| [starlette](https://pypi.org/project/starlette/0.46.2/) | 0.46.2 | BSD-3-Clause |
| [tqdm](https://pypi.org/project/tqdm/4.70.1/) | 4.70.1 | MPL-2.0 AND MIT |
| [types-deprecated](https://pypi.org/project/types-deprecated/1.3.1.20260928/) | 1.3.1.20260928 | Apache-2.0 |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [tzdata](https://pypi.org/project/tzdata/2026.5/) | 2026.5 | Apache-2.0; PyPI metadata |
| [urllib3](https://pypi.org/project/urllib3/2.8.0/) | 2.8.0 | MIT |
| [uvicorn](https://pypi.org/project/uvicorn/0.30.6/) | 0.30.6 | BSD-3-Clause |
| [uvloop](https://pypi.org/project/uvloop/0.23.0/) | 0.23.0 | MIT License |
| [watchfiles](https://pypi.org/project/watchfiles/1.3.0/) | 1.3.0 | MIT |
| [websockets](https://pypi.org/project/websockets/15.0.1/) | 15.0.1 | BSD-3-Clause |
| [wrapt](https://pypi.org/project/wrapt/2.5.1/) | 2.5.1 | BSD-2-Clause |
| [x402-xrpl](https://pypi.org/project/x402-xrpl/0.3.4/) | 0.3.4 | MIT (verified installed LICENSE) |
| [xrpl-py](https://pypi.org/project/xrpl-py/5.2.0/) | 5.2.0 | ISC |

Apply the selected release license consistently to the root license file, package
metadata and artifacts using `docs/license-plan.md`. Preserve applicable third-party
notices. This inventory is shipped in the sdist, wheel and container.
