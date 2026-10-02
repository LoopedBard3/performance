# BCS installer feeds

The five ordinary CoreCLR and five ASP.NET Core configurations additionally publish
`BcsInstallers_{os}_{arch}_Release_{coreclr|aspnetcore}` pipeline artifacts.
Existing raw artifacts, aliases, `buildInfo.json`, and their overwrite behavior are unchanged.
Android, iOS, Mono, WASM, and special CoreCLR configurations do not publish installer feeds.

Use `builds/{repo}/buildArtifacts/{full-sha}/{config}/install` as the `dotnet-install`
`AzureFeed` root. Paths relative to that root are:

| Product | Version file | Archives |
| --- | --- | --- |
| .NET | `Runtime/main/latest.version` | `Runtime/{V}/dotnet-runtime-{V}-{RID}.{tar.gz|zip}` |
| ASP.NET | `aspnetcore/Runtime/main/latest.version` | `aspnetcore/Runtime/{V}/aspnetcore-runtime-{V}-{RID}.{tar.gz|zip}` |

Read the last nonempty line of the version file, then use that exact `Version`
with the existing installer and the appropriate runtime/architecture. The version
file may also contain a preceding source SHA. Windows publishes both zip and
tar.gz for old and current PowerShell installers; Linux publishes tar.gz.
Original `Microsoft.{NETCore|AspNetCore}.App.Runtime.{RID}.{V}.nupkg` files can
also appear in the respective version folder. They are plain package downloads,
not a NuGet service or a complete SDK pack set.

## ASP.NET archive contents

The existing primary build already creates these archives in
`artifacts/packages/Release/Shipping`. ASP.NET's
[`aspnetcore-runtime.proj`](https://github.com/dotnet/aspnetcore/blob/main/src/Framework/App.Runtime/src/aspnetcore-runtime.proj)
includes the base .NET runtime/host **and** `shared/Microsoft.AspNetCore.App/{V}`.
Arcade's archive targets run during `Build`, not NuGet `Pack`;
`OnlyPackPlatformSpecificPackages=true` need not change. The build already
downloads its pinned base runtime through its existing public feed configuration.
Staging copies archives and the runtime pack verbatim, without extracting or repacking.

Consumers selecting .NET and ASP.NET independently must install ASP.NET into a
**fresh separate directory**, then promote only `shared/Microsoft.AspNetCore.App`.
Installing directly into the selected runtime directory can overwrite the host
and base runtime, or skip an already-present same-version framework.
The archive's runtimeconfig/deps metadata is unchanged; selected versions must
still be compatible. Normal framework-dependent compilation uses the fixed SDK's
reference packs, not reference packs from these commits. Runtime-pack downloads
alone do not guarantee self-contained compatibility with every SDK.

## Publication and limitations

All required archives must exist locally before upload. Installer files are
create-only, and `latest.version` is uploaded **last**, after every file succeeds.
The first completed publication for a commit/configuration wins: an existing
version marker and its original version/files are left unchanged, even if a
later rebuild produces a different version.
An upload failure leaves no completion marker; partial uploads are not
automatically resumed or overwritten. An operator must clean up an incomplete
installer prefix before retrying. The original raw uploads remain overwrite-enabled.

Only updated producers supply this contract. Historical builds without the
installer artifact need rebuilding; there is no raw-artifact fallback.
ASP.NET's SHA remains its repository-resource commit, not the performance commit.
No deployment or existing cache migration is implied by these pipeline changes.

Local staging/upload fixtures use PowerShell 7, pytest, and PyYAML:
`python -m pytest scripts/tests/test_bcs_installers.py -q`. Azure CLI is mocked;
these tests do not deploy or upload anything.
