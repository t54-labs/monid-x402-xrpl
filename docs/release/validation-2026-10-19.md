# Release preparation validation — 2026-10-19

Base: `6221d9241b0433fa881afb296a2c3cf59319fb0f`. T54 has confirmed open-source
publication. These are local technical checks, separate from production deployment
validation. Exact combined commits, history-scan coverage and remote-operation
results are reported separately to the release coordinator.

| Check | Result |
| --- | --- |
| Remote visibility/default head | Private; main matched the base above at inspection |
| Existing remote history | Gitleaks 8.30.1, official checksum-verified binary; built-in rules plus XRPL seed candidates; 6 advertised branch/PR refs, 14 reachable commits, 86 unique blobs, 0 findings; see JSON evidence |
| Offline unit suite | `pytest -q`: 130 passed, including exact MIT/metadata inclusion and corrupt/missing license rejection; network/DNS blocked by suite; dotenv disabled |
| Lint and patch whitespace | `ruff check .` and `git diff --check`: passed |
| Source/wheel build | `uv build --offline`: passed using cached build dependencies; exact root MIT text in sdist and wheel, `License-Expression: MIT`, both license-file declarations and notice contents verified |
| Artifact boundary checker | sdist/wheel paths checked; third-party notices present; seven unsafe/private-path rejection cases passed |
| Packaged smoke | All ten gateway modules imported from built wheel; five mocked route checks and two reconciliation CLI checks passed; network/dotenv disabled |
| Docker transmitted context | Actual BuildKit scratch/COPY context export: 27 source/build files including exact LICENSE; all seven synthetic canaries absent |
| GCloud upload list | `gcloud meta list-files-for-upload` with an isolated local config: 31 build files including LICENSE; all seven canaries absent; build example present, GKE manifests absent |
| Image build | Local MIT image build passed; review image `sha256:c89b94714846a313124ec36c747c8c67ecbe912ecbd61b2d5dcd0f2694e7323c` |
| Image smoke | Read-only container, `--network none`, blocked sockets, tmpfs-only runtime data: gateway import passed; no dotenv/PEM canary; exact root/installed MIT text, MIT expression and both installed license-file declarations verified |
| Image dependency notices | License files present for x402-xrpl, xrpl-py, certifi, psycopg and psycopg-binary; not a full binary/OS license audit |
| Examples | Eight YAML files parsed; `kubectl kustomize deploy/examples/gke` rendered offline; personal paths, original project identifier and operator receiver absent from example files |
| PR #1 integration | Seven resource files preserved after documented identifier/default changes; normal local merges retain original heads/main; four conflicts explicitly resolved; obsolete duplicate root/GKE paths removed; `gateway/` matches main including PR #2 exactly |
| Applicable GitHub CI/rules | No required checks, workflow/check-run/status entries, rulesets or main protection were present at inspection; an empty combined-status response marked pending is not an actual queued CI run |
| Deployment source | Read-only Railway service settings show an unconnected Source with Connect Repo/Connect Image and CLI root-directory settings; no production setting was changed |
| Dependency inventory | All 68 locked third-party entries covered by installed exact-version metadata or exact PyPI release metadata; build backend and base-image/OS/native subcomponents separately scoped |
| Original worktree | Status unchanged; no production configuration or service modified |

The canaries were synthetic files created **only in the isolated preparation clone**:
`.env`, `.env.production`, `data/e2e-wallets.json`, `data/gateway.sqlite3`,
`gateway/__pycache__/canary.pyc`, `scripts/release-canary.pem`, and
`release-canary.log`. No real developer/production credential, wallet or database
was opened or copied. The initial context probe caught the inherited ignore
pattern's directory-negation behavior; the final explicit subtree exclusions and
credential/runtime-file denies were tested against Docker and GCloud independently.

No paid API/E2E/chain test, GCP apply/dry-run, Railway operation, remote image push,
repository security-setting change or visibility change was performed. Existing
historical identifiers remain in the refs listed in the release checklist.
GitHub-managed/hidden/deleted refs, unreachable objects, external clones, GitHub
text/attachments/Actions artifacts and actual production state remain unverified.
Static secret scanning and package metadata checks do not establish complete
vulnerability absence, legal clearance or production readiness.
