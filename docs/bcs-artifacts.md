# BCS framework packages

The five ordinary CoreCLR and five ASP.NET desktop lanes publish complete
package ZIPs alongside existing raw artifacts. The upload prefix remains
`builds/{runtime|aspnetcore}/buildArtifacts/{sourceSha}/{configKey}/`.

| Producer | Additional blob | ZIP root contents |
| --- | --- | --- |
| Runtime | `RuntimeDistribution_{linux|windows}_{arch}_Release_coreclr.zip` | Original `dotnet-runtime` bundle and Runtime, Ref, Host nupkgs; Crossgen2 when produced. |
| ASP.NET | `FrameworkPackages_{linux|windows}_{arch}_Release_aspnetcore.zip` | Original runtime and matching-version Ref nupkgs. |

Runtime publishes its ZIP in the same-named `RuntimeDistribution_*` pipeline
artifact. ASP.NET adds its ZIP to the existing `BuildArtifacts_*` pipeline
artifact and retains the byte-identical fixed-name runtime nupkg alias.
Package filenames and bytes inside each ZIP are unchanged; there is no manifest,
sidecar, or content-addressed upload tree. Metadata validation belongs to consumers.

The existing `artifacts`/`files` uploader handles these files. Its `immutableFiles`
list marks only the new complete ZIPs for block-blob conditional creation
(`--overwrite false --if-none-match '*'`). An existing destination fails clearly,
even for an identical retry; there is no download/hash reconciliation. Existing
raw files still use `--overwrite true`, with unchanged names and layouts.
Unsupported Mono, WASM, mobile, and interpreter lanes remain raw-only.

ASP.NET primary Windows x64 and independent Linux x64/arm64 builds pack Ref by
omitting `OnlyPackPlatformSpecificPackages=true`. Windows x86/arm64 cross-pack
steps retain that filter and reuse the primary build's Ref. Staging requires
exactly one non-symbol runtime pack and one Ref pack, with matching filename
versions. Extra managed packaging time/storage has not been measured.

The ASP.NET repository resource SHA still determines its upload prefix; it is not
the performance pipeline SHA. Source/public guards and registration remain
unchanged. Runtime's matching producer update must accompany this template
update. Existing raw-only history needs original-package backfill or a clear
unsupported error from package consumers, not reconstruction from mutable feeds.

Offline staging/upload-wiring tests:

```powershell
python -m pytest scripts\tests\test_bcs_artifacts.py -q
```
