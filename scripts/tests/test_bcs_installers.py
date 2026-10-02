import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / "eng" / "pipelines"
VERSION = "12.0.0-alpha.1.12345.1"
RIDS = ["linux-x64", "linux-arm64", "win-x64", "win-x86", "win-arm64"]


def load_pipeline(name):
    return yaml.safe_load((PIPELINES / name).read_text())


def installer_files(directory, rid, product="aspnetcore/Runtime"):
    directory.mkdir(parents=True, exist_ok=True)
    framework = "AspNetCore" if product.startswith("aspnetcore") else "NETCore"
    prefix = "aspnetcore-runtime" if framework == "AspNetCore" else "dotnet-runtime"
    files = {
        f"shared/Microsoft.{framework}.App/{VERSION}/fixture.dll": b"framework",
        "dotnet.exe" if rid.startswith("win-") else "dotnet": b"host",
    }
    archive = directory / f"{prefix}-{VERSION}-{rid}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
    if rid.startswith("win-"):
        with zipfile.ZipFile(directory / f"{prefix}-{VERSION}-{rid}.zip", "w") as output:
            for name, content in files.items():
                output.writestr(name, content)
    package = directory / f"Microsoft.{framework}.App.Runtime.{rid}.{VERSION}.nupkg"
    with zipfile.ZipFile(package, "w") as output:
        output.writestr(f"runtimes/{rid}/lib/net12.0/fixture.dll", b"runtime pack")
    return package


def stage(shipping, destination, rid):
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File",
         str(PIPELINES / "tools" / "stage-bcs-nupkg-aspnetcore.ps1"),
         "-Rids", rid, "-ShippingDir", str(shipping), "-StagingRoot", str(destination)],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize("rid", RIDS)
def test_staging_preserves_original_archives_and_raw_alias(tmp_path, rid):
    shipping = tmp_path / "shipping"
    package = installer_files(shipping, rid)
    original = {path.name: path.read_bytes() for path in shipping.iterdir()}
    output = tmp_path / "staging"
    result = stage(shipping, output, rid)
    assert result.returncode == 0, result.stdout + result.stderr
    os_name, arch = rid.split("-")
    os_name = "windows" if os_name == "win" else os_name
    suffix = f"{os_name}_{arch}_Release_aspnetcore"
    raw = output / f"BuildArtifacts_{suffix}" / f"BuildArtifacts_{suffix}.nupkg"
    assert raw.read_bytes() == original[package.name]
    product = output / f"BcsInstallers_{suffix}" / "aspnetcore" / "Runtime"
    assert (product / "main" / "latest.version").read_text().strip() == VERSION
    assert {p.name: p.read_bytes() for p in (product / VERSION).iterdir()} == original
    assert {p.name: p.read_bytes() for p in shipping.iterdir()} == original


@pytest.mark.parametrize("problem", ["missing-pack", "duplicate-pack", "missing-tar", "missing-zip"])
def test_incomplete_staging_fails_without_version_marker(tmp_path, problem):
    shipping = tmp_path / "shipping"
    package = installer_files(shipping, "win-x64")
    if problem == "missing-pack":
        package.unlink()
    elif problem == "duplicate-pack":
        package.with_name(package.name.replace(VERSION, "12.0.0")).write_bytes(b"duplicate")
    else:
        extension = "tar.gz" if problem == "missing-tar" else "zip"
        (shipping / f"aspnetcore-runtime-{VERSION}-win-x64.{extension}").unlink()
    output = tmp_path / "staging"
    result = stage(shipping, output, "win-x64")
    assert result.returncode != 0
    assert not list(output.rglob("latest.version"))


def test_uploader_mappings_and_primary_build_flags():
    jobs = load_pipeline("upload-build-artifacts-jobs.yml")["jobs"]
    selected = {}
    for branch in jobs:
        for job in next(iter(branch.values())):
            parameters = job["parameters"]
            assert parameters["repoName"] == "${{ parameters.repoName }}"
            assert parameters["sha"] == "${{ parameters.sha }}"
            for artifact in parameters["artifacts"]:
                if "installerProduct" in artifact:
                    selected[parameters["buildType"]] = artifact
                else:
                    assert parameters["buildType"] not in {
                        f"{product}_{rid.split('-')[1]}_{'windows' if rid.startswith('win-') else 'linux'}"
                        for product in ["aspnetcore", "coreclr"] for rid in RIDS
                    }
    assert len(selected) == 10
    producer = (PIPELINES / "aspnetcore-perf-build-jobs.yml").read_text()
    assert producer.count("OnlyPackPlatformSpecificPackages=true") == 5
    for product in ["aspnetcore", "coreclr"]:
        for rid in RIDS:
            os_name, arch = rid.split("-")
            os_name = "windows" if os_name == "win" else os_name
            artifact = selected[f"{product}_{arch}_{os_name}"]
            assert artifact["installerRid"] == rid
            assert artifact["installerProduct"] == (
                "Runtime" if product == "coreclr" else "aspnetcore/Runtime")
            suffix = f"{os_name}_{arch}_Release_{product}"
            assert artifact["artifactName"] == f"BuildArtifacts_{suffix}"
            extension = "nupkg" if product == "aspnetcore" else (
                "zip" if os_name == "windows" else "tar.gz")
            assert artifact["files"] == [f"BuildArtifacts_{suffix}.{extension}"]
            if product == "aspnetcore":
                assert f"artifactName: BcsInstallers_{suffix}" in producer


def uploader_script():
    pipeline = load_pipeline("templates/upload-build-artifacts-job.yml")
    job = pipeline["jobs"][0]["${{ each artifact in parameters.artifacts }}"][0]
    conditional = job["steps"][-1]
    steps = conditional["${{ if in(artifact.installerProduct, 'Runtime', 'aspnetcore/Runtime') }}"]
    return steps[-1]["inputs"]["inlineScript"]


@pytest.mark.parametrize("product,rid", [("Runtime", "win-x64"), ("aspnetcore/Runtime", "linux-arm64")])
@pytest.mark.parametrize("scenario", [
    "success", "cache-hit", "upload-failure", "partial-upload-failure", "marker-failure",
    "lookup-failure", "invalid-lookup", "missing-archive",
])
def test_actual_inline_uploader_orders_marker_last(tmp_path, product, rid, scenario):
    workspace = tmp_path / "workspace"
    feed = workspace / "installers" / product
    installer_files(feed / VERSION, rid, product)
    (feed / "main").mkdir(parents=True)
    (feed / "main" / "latest.version").write_text(f"{'a' * 40}\n{VERSION}\n")
    if scenario == "missing-archive":
        next((feed / VERSION).glob("*.tar.gz")).unlink()
    calls = tmp_path / "calls.jsonl"
    script = uploader_script()
    for key, value in {
        "$(Pipeline.Workspace)": workspace.as_posix(),
        "${{ replace(artifact.artifactName, 'BuildArtifacts_', 'BcsInstallers_') }}": "installers",
        "${{ artifact.installerProduct }}": product,
        "${{ artifact.installerRid }}": rid,
        "${{ parameters.repoName }}": "aspnetcore" if product.startswith("aspnetcore") else "runtime",
        "${{ parameters.sha }}": "a" * 40,
        "${{ parameters.buildType }}": "fixture_config",
    }.items():
        script = script.replace(key, value)
    stub = r"""
$script:uploadCount = 0
function az {
    $arguments = @($args)
    ConvertTo-Json -InputObject $arguments -Compress | Add-Content -LiteralPath $env:BCS_CALLS
    $global:LASTEXITCODE = 0
    if ($arguments[2] -eq 'exists') {
        if ($env:BCS_SCENARIO -eq 'lookup-failure') { $global:LASTEXITCODE = 1; return }
        if ($env:BCS_SCENARIO -eq 'invalid-lookup') { return 'unexpected' }
        if ($env:BCS_SCENARIO -eq 'cache-hit') { return 'true' }
        return 'false'
    }
    $script:uploadCount++
    if ($env:BCS_SCENARIO -eq 'upload-failure' -or
        ($env:BCS_SCENARIO -eq 'partial-upload-failure' -and $script:uploadCount -eq 2) -or
        ($env:BCS_SCENARIO -eq 'marker-failure' -and
            $arguments[$arguments.IndexOf('--name') + 1].EndsWith('/latest.version'))) {
        $global:LASTEXITCODE = 1
    }
}
"""
    executable = tmp_path / "uploader.ps1"
    executable.write_text(stub + script)
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(executable)], capture_output=True, text=True,
        env={**os.environ, "BCS_CALLS": str(calls), "BCS_SCENARIO": scenario},
    )
    requests = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    uploads = [request for request in requests if request[2] == "upload"]
    if scenario in {"success", "cache-hit"}:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
    names = [request[request.index("--name") + 1] for request in uploads]
    if scenario in {"success", "marker-failure"}:
        assert len(uploads) == (4 if rid.startswith("win-") else 3)
        assert names[-1].endswith(f"/install/{product}/main/latest.version")
        assert all(f"/install/{product}/{VERSION}/" in name for name in names[:-1])
    else:
        assert not any(name.endswith("latest.version") for name in names)
    if scenario in {"cache-hit", "lookup-failure", "invalid-lookup", "missing-archive"}:
        assert not uploads
    for request in uploads:
        assert request[request.index("--overwrite") + 1] == "false"
        assert request[request.index("--if-none-match") + 1] == "*"
