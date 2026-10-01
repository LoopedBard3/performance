from pathlib import Path
import subprocess
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / "eng" / "pipelines"
SCRIPT = PIPELINES / "tools" / "stage-bcs-nupkg-aspnetcore.ps1"
RIDS = ["linux-x64", "linux-arm64", "win-x64", "win-x86", "win-arm64"]
VERSION = "12.0.0-ci"


def packages(directory, rid):
    directory.mkdir()
    runtime = directory / f"Microsoft.AspNetCore.App.Runtime.{rid}.{VERSION}.nupkg"
    reference = directory / f"Microsoft.AspNetCore.App.Ref.{VERSION}.nupkg"
    for path in (runtime, reference):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("original-metadata", path.name)
    return runtime, reference


def stage(shipping, output, rid):
    return subprocess.run([
        "pwsh", "-NoProfile", "-File", str(SCRIPT), "-Rids", rid,
        "-ShippingDir", str(shipping), "-StagingRoot", str(output),
    ], capture_output=True, text=True)


@pytest.mark.parametrize("rid", RIDS)
def test_stage_preserves_raw_alias_and_original_package_bytes_at_zip_root(tmp_path, rid):
    shipping, output = tmp_path / "shipping", tmp_path / "staged"
    runtime, reference = packages(shipping, rid)
    (shipping / runtime.name.replace(".nupkg", ".symbols.nupkg")).write_bytes(b"ignored")
    (shipping / reference.name.replace(".nupkg", ".symbols.nupkg")).write_bytes(b"ignored")
    for _ in range(2):
        result = stage(shipping, output, rid)
        assert result.returncode == 0, result.stderr
        os_name, arch = rid.replace("win-", "windows-").split("-")
        artifact = f"BuildArtifacts_{os_name}_{arch}_Release_aspnetcore"
        directory = output / artifact
        transport = directory / f"FrameworkPackages_{os_name}_{arch}_Release_aspnetcore.zip"
        assert {p.name for p in directory.iterdir()} == {artifact + ".nupkg", transport.name}
        assert (directory / (artifact + ".nupkg")).read_bytes() == runtime.read_bytes()
        with zipfile.ZipFile(transport) as archive:
            assert sorted(archive.namelist()) == sorted([runtime.name, reference.name])
            for package in (runtime, reference):
                assert archive.read(package.name) == package.read_bytes()
                assert archive.getinfo(package.name).compress_type == zipfile.ZIP_STORED


@pytest.mark.parametrize("problem", ["missing-runtime", "missing-ref", "extra-runtime", "extra-ref", "wrong-version"])
def test_missing_ambiguous_or_mismatched_packages_fail(tmp_path, problem):
    shipping, output = tmp_path / "shipping", tmp_path / "staged"
    runtime, reference = packages(shipping, "linux-x64")
    if problem.startswith("missing-"):
        (runtime if problem == "missing-runtime" else reference).unlink()
    elif problem.startswith("extra-"):
        source = runtime if problem == "extra-runtime" else reference
        source.with_name(source.name.replace(VERSION, "12.0.0-other")).write_bytes(source.read_bytes())
    else:
        reference.rename(reference.with_name(reference.name.replace(VERSION, "11.0.0-ci")))
    result = stage(shipping, output, "linux-x64")
    assert result.returncode != 0
    assert not list(output.rglob("FrameworkPackages_*.zip"))


def load(name):
    return yaml.safe_load((PIPELINES / name).read_text())


def test_existing_uploader_adds_only_complete_zip_files_for_supported_lanes():
    supported = set()
    for group in load("upload-build-artifacts-jobs.yml")["jobs"]:
        for jobs in group.values():
            for job in jobs:
                parameters = job["parameters"]
                config = parameters["buildType"]
                artifacts = parameters["artifacts"]
                if config in {f"{flavor}_{rid.split('-')[1]}_{'windows' if rid.startswith('win-') else 'linux'}"
                              for flavor in ("coreclr", "aspnetcore") for rid in RIDS}:
                    supported.add(config)
                    flavor, arch, os_name = config.split("_")
                    raw = f"BuildArtifacts_{os_name}_{arch}_Release_{flavor}"
                    prefix = "RuntimeDistribution" if flavor == "coreclr" else "FrameworkPackages"
                    transport = f"{prefix}_{os_name}_{arch}_Release_{flavor}"
                    assert parameters["immutableFiles"] == [transport + ".zip"]
                    extension = "nupkg" if flavor == "aspnetcore" else "zip" if os_name == "windows" else "tar.gz"
                    assert artifacts[0]["artifactName"] == raw
                    assert artifacts[0]["files"][0] == raw + "." + extension
                    if flavor == "coreclr":
                        assert artifacts == [
                            {"artifactName": raw, "files": [raw + "." + extension]},
                            {"artifactName": transport, "files": [transport + ".zip"]},
                        ]
                    else:
                        assert artifacts == [{"artifactName": raw, "files": [raw + ".nupkg", transport + ".zip"]}]
                else:
                    assert "immutableFiles" not in parameters
                assert parameters["sha"] == "${{ parameters.sha }}"
    assert len(supported) == 10


def test_uploader_keeps_raw_command_and_only_adds_conditional_create():
    template = load(Path("templates") / "upload-build-artifacts-job.yml")
    assert template["parameters"]["immutableFiles"] == []
    job = next(iter(template["jobs"][0].values()))[0]
    assert job["condition"] == "eq(stageDependencies.Build.${{ parameters.dependencyJobName }}.result, 'Succeeded')"
    assert job["steps"][0] == {"checkout": "none"}
    task = job["steps"][2]["${{ each fileName in artifact.files }}"][-1]
    inputs = task["inputs"]
    raw = inputs["${{ else }}"]["inlineScript"].strip()
    expected = ("az storage blob upload --auth-mode login --account-name pvscmdupload --container-name '$web' "
                '--file "$(Pipeline.Workspace)/${{ artifact.artifactName }}/${{ fileName }}" '
                '--name "builds/${{ parameters.repoName }}/buildArtifacts/${{ parameters.sha }}/'
                '${{ parameters.buildType }}/${{ fileName }}"')
    assert raw == expected + " --overwrite true"
    immutable = inputs["${{ if containsValue(parameters.immutableFiles, fileName) }}"]["inlineScript"]
    assert immutable.splitlines()[0] == expected + " --type block --overwrite false --if-none-match '*'"
    assert '$LASTEXITCODE -ne 0' in immutable and "throw" in immutable


def test_aspnet_ref_pack_build_and_source_guards_are_preserved():
    jobs = [job for group in load("aspnetcore-perf-build-jobs.yml")["jobs"]
            for members in group.values() for job in members]
    assert len(jobs) == 3
    for job in jobs:
        primary = next(step["script"] for step in job["steps"] if "script" in step)
        assert "-all" in primary and "OnlyPackPlatformSpecificPackages" not in primary
    text = (PIPELINES / "aspnetcore-perf-build-jobs.yml").read_text()
    assert text.count("/p:OnlyPackPlatformSpecificPackages=true") == 2
    root = load("aspnetcore-perf-build.yml")
    assert next(v["value"] for v in root["variables"] if v.get("name") == "_AspNetCoreSha") == "$[ resources.repositories.aspnetcore.version ]"
    build = next(stage for stage in root["stages"] if stage.get("stage") == "Build")
    assert build["condition"] == ("and(ne(variables['System.TeamProject'], 'public'), "
                                  "or(eq(variables['Build.Reason'], 'IndividualCI'), eq(variables['Build.Reason'], 'Manual')))")
    upload = next(stage for group in root["stages"] if "stage" not in group
                  for members in group.values() for stage in members if stage["stage"] == "UploadArtifacts")
    assert upload["dependsOn"] == ["Build", "RegisterBuild"] and upload["condition"] == "succeeded()"
    assert upload["jobs"][0]["parameters"]["sha"] == "$(_AspNetCoreSha)"
