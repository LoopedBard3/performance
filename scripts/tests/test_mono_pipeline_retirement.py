"""Offline contracts for pipeline selection; external runtime templates are boundaries."""

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.channel_map import ChannelMap
from scripts.run_performance_job import get_run_configurations


ROOT = Path(__file__).resolve().parents[2]
PIPELINES = ROOT / "eng" / "pipelines"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
MONO_BUILDS = {
    "wasm", "mono_x64_linux", "mono_arm64_linux",
    "monoAot_x64_linux", "monoAot_arm64_linux",
}
CURRENT_BUILDS = {
    "coreclr_x64_linux", "coreclr_arm64_linux", "coreclr_x64_windows",
    "coreclr_arm64_windows", "coreclr_x86_windows", "coreclr_arm64_android",
    "nativeAot_arm64_ios", "coreclr_arm64_ios", "wasm_coreclr",
    "coreclr_r2r_interpreter",
}
LOCAL_TEMPLATES = {
    "templates/runtime-mono-jobs.yml",
    "templates/runtime-wasm-build-jobs.yml",
    "runtime-ios-scenarios-perf-jobs.yml",
}


def evaluate(expression, context):
    """Evaluate only the compile-time expression subset used by these templates."""
    expression = re.sub(r"\b(and|or|not|in)\(", r"_\1(", expression)

    def equal(left, right):
        if isinstance(left, str) and isinstance(right, str):
            return left.lower() == right.lower()
        return left == right

    functions = {
        "_and": lambda *args: all(args),
        "_or": lambda *args: any(args),
        "_not": lambda value: not value,
        "_in": lambda value, *choices: any(equal(value, choice) for choice in choices),
        "notin": lambda value, *choices: not any(equal(value, choice) for choice in choices),
        "eq": equal,
        "ne": lambda left, right: not equal(left, right),
        "coalesce": lambda *args: next((arg for arg in args if arg not in ("", None)), ""),
        "containsvalue": lambda values, value: value in values,
        "startswith": lambda value, prefix: value.startswith(prefix),
        "replace": lambda value, old, new: value.replace(old, new),
        "split": lambda value, separator: value.split(separator),
        "format": lambda value, *args: value.format(*args),
    }

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id.lower() in ("true", "false"):
                return node.id.lower() == "true"
            return context[node.id]
        if isinstance(node, ast.Attribute):
            value = visit(node.value)
            return "" if value is None else value.get(node.attr, "")
        if isinstance(node, ast.Subscript):
            value, key = visit(node.value), visit(node.slice)
            return value.get(key, "") if isinstance(value, dict) else value[key]
        if isinstance(node, ast.Call):
            return functions[node.func.id.lower()](*(visit(arg) for arg in node.args))
        raise AssertionError(f"Unsupported pipeline expression: {ast.dump(node)}")

    return visit(ast.parse(expression, mode="eval").body)


def expand(value, context):
    if isinstance(value, str):
        match = EXPRESSION.fullmatch(value)
        if match:
            return evaluate(match[1], context)
        return EXPRESSION.sub(lambda match: str(evaluate(match[1], context)), value)
    if not isinstance(value, (dict, list)):
        return value

    result = {} if isinstance(value, dict) else []

    def append(expanded):
        if isinstance(result, dict):
            result.update(expanded)
        else:
            result.extend(expanded if isinstance(expanded, list) else [expanded])

    matched = False
    entries = value.items() if isinstance(value, dict) else [(None, item) for item in value]
    for key, item in entries:
        if key is None and isinstance(item, dict) and len(item) == 1:
            candidate = next(iter(item))
            if EXPRESSION.fullmatch(candidate):
                key, item = candidate, item[candidate]
        match = EXPRESSION.fullmatch(key) if isinstance(key, str) else None
        directive = match[1] if match else ""
        if directive.startswith("if "):
            matched = evaluate(directive[3:], context)
            if matched:
                append(expand(item, context))
        elif directive.startswith("elseif "):
            if not matched and evaluate(directive[7:], context):
                append(expand(item, context))
                matched = True
        elif directive == "else":
            if not matched:
                append(expand(item, context))
            matched = True
        elif directive.startswith("each "):
            name, expression = directive[5:].split(" in ", 1)
            sequence = evaluate(expression, context)
            if isinstance(sequence, dict):
                sequence = [{"key": key, "value": item} for key, item in sequence.items()]
            for entry in sequence:
                append(expand(item, {**context, name: entry}))
        elif directive == "insert":
            append(expand(item, context))
        elif isinstance(result, dict):
            result[expand(key, context)] = expand(item, context)
        else:
            append(expand(item, context))
    return result


def jobs(filename, branch="refs/heads/main", parameters=None, variables=None):
    data = yaml.safe_load((PIPELINES / filename).read_text(encoding="utf-8"))
    defaults = data.get("parameters", {})
    if isinstance(defaults, list):
        defaults = {item["name"]: item.get("default", "") for item in defaults}
    context = {
        "parameters": {**defaults, **(parameters or {})},
        "variables": {
            "Build.SourceBranch": branch,
            "Build.DefinitionName": "perf",
            **(variables or {}),
        },
    }
    result = []
    for job in expand(data["jobs"], context):
        template = job.get("template", "").split("@")[0]
        local = template.removeprefix("/eng/pipelines/")
        if local in LOCAL_TEMPLATES:
            result.extend(jobs(local, branch, job["parameters"], variables))
        else:
            result.append(job)
    return result


def run_parameters(expanded):
    parameters = [
        {key.lower(): value for key, value in job.get("parameters", {}).items()}
        for job in expanded
    ]
    return [
        {key.lower(): value for key, value in parameter["jobparameters"].items()}
        for parameter in parameters
        if "jobparameters" in parameter
        and "runtime-perf-job.yml" in parameter.get("jobtemplate", "")
    ]


def produced_jobs(expanded):
    """Job-name contracts of the runtime templates inspected for this change."""
    result = set()
    for job in expanded:
        template = job.get("template", "").split("@")[0]
        parameters = job.get("parameters", {})
        if template.endswith("perf-coreclr-build-jobs.yml"):
            for platform in ("linux_x64", "linux_arm64", "windows_x64", "windows_arm64", "windows_x86"):
                if parameters.get(platform):
                    result.add(f"build_{platform}_release_coreclr")
            if parameters.get("android_arm64"):
                result.add("build_android_arm64_release_AndroidCoreCLR")
            if parameters.get("coreclr_r2r_interpreter"):
                result.add("build_linux_arm64_release_coreclr_r2r_interpreter")
        elif template.endswith("perf-mono-build-jobs.yml"):
            for arch in ("x64", "arm64"):
                if parameters.get(f"mono_{arch}"):
                    result.add(f"build_linux_{arch}_release_mono")
                if parameters.get(f"monoAot_{arch}"):
                    result.add(f"build_linux_{arch}_release_AOT")
        elif template.endswith("perf-ios-scenarios-build-jobs.yml"):
            for flavor, suffix in [("coreclr", "iOSCoreCLR"), ("nativeAot", "iOSNativeAOT")]:
                if parameters.get(flavor):
                    result.add(f"build_ios_arm64_release_{suffix}")
        elif template.endswith("perf-wasm-build-jobs.yml"):
            # Verified release/11.0 wrapper builds both payloads.
            result.update(["build_browser_wasm_linux_Release_wasm",
                           "build_browser_wasm_linux_Release_wasm_coreclr"])
        elif parameters.get("platforms") == ["browser_wasm"]:
            result.add(f"build_browser_wasm_linux_{parameters['buildConfig']}_"
                       f"{parameters['jobParameters']['nameSuffix']}")
    return {name.lower() for name in result}


@pytest.mark.parametrize("branch", [
    "refs/heads/main", "refs/pull/123/merge", "refs/heads/feature",
    "refs/heads/release/12.0", "refs/heads/release/13.0",
])
def test_current_runtime_never_selects_mono(branch):
    for filename, parameters in [
        ("runtime-perf-jobs.yml", {}),
        ("runtime-slow-perf-jobs.yml", {"runPrivateJobs": True, "runScheduledJobs": True}),
        ("runtime-wasm-perf-jobs.yml", {"runProfile": "v8"}),
        ("runtime-wasm-perf-jobs.yml", {"runProfile": "non-v8"}),
        ("runtime-perf-build-jobs.yml", {"buildType": sorted(MONO_BUILDS | CURRENT_BUILDS)}),
    ]:
        expanded = jobs(filename, branch, parameters)
        assert all(run.get("runtimetype") not in ("mono", "wasm")
                   for run in run_parameters(expanded))
        templates = [job.get("template", "") for job in expanded]
        assert not any("perf-mono-build-jobs" in template for template in templates)
        assert not any("perf-build-jobs.yml@" in template for template in templates)
        assert not any("perf-wasm-build-jobs.yml@" in template for template in templates)
        assert not any("perf-arm64-build-jobs.yml@" in template for template in templates)


@pytest.mark.parametrize("branch", [
    "refs/heads/release/8.0", "refs/heads/release/9.0", "refs/heads/release/10.0",
    "refs/heads/release/11.0", "refs/heads/release/11.0-rc2",
    "refs/heads/internal/release/11.0", "release/11.0", "internal/release/11.0-rc2",
])
def test_older_runtime_keeps_mono_even_from_performance_main(branch):
    parameters = {"runtimeBranch": branch}
    desktop = jobs("runtime-perf-jobs.yml", parameters=parameters)
    mono = [run for run in run_parameters(desktop) if run.get("runtimetype") == "mono"]
    assert {run.get("codegentype", "JIT") for run in mono} == {"JIT", "Interpreter", "AOT"}
    assert any("perf-build-jobs.yml@" in job.get("template", "") for job in desktop)

    slow = jobs("runtime-slow-perf-jobs.yml", parameters={
        **parameters, "runPrivateJobs": True, "runScheduledJobs": True,
    })
    mono = [run for run in run_parameters(slow) if run.get("runtimetype") == "mono"]
    assert {run.get("codegentype", "JIT") for run in mono} == {"JIT", "Interpreter", "AOT"}

    for profile, engine in [("v8", "v8"), ("non-v8", "javascriptcore")]:
        wasm = jobs("runtime-wasm-perf-jobs.yml", parameters={**parameters, "runProfile": profile})
        mono = [run for run in run_parameters(wasm) if run.get("runtimetype") == "wasm"]
        assert {run["codegentype"] for run in mono} == {"wasm", "aot"}
        assert {run["javascriptengine"] for run in mono} == {engine}
        assert {run["runtimetype"] for run in run_parameters(wasm)} == {"wasm"}
        assert any("perf-wasm-build-jobs.yml@" in job.get("template", "") for job in wasm)


def test_existing_artifact_branch_controls_wasm_runs():
    for branch, expected in [("release/11.0", "wasm"), ("main", "wasm_coreclr")]:
        expanded = jobs("runtime-wasm-perf-jobs.yml", branch="refs/heads/release/10.0", parameters={
            "runProfile": "v8",
            "downloadSpecificBuild": {"buildId": "test-only", "branchName": f"refs/heads/{branch}"},
        })
        assert {run["runtimetype"] for run in run_parameters(expanded)} == {expected}
        assert not any("build-jobs.yml@" in job.get("template", "") for job in expanded)


def test_known_release_pr_target_and_custom_runtime_override():
    release_pr = jobs("runtime-perf-jobs.yml", branch="refs/pull/123/merge", variables={
        "System.PullRequest.TargetBranch": "release/11.0",
    })
    assert len([run for run in run_parameters(release_pr) if run.get("runtimetype") == "mono"]) == 3
    cached_pr = jobs("runtime-wasm-perf-jobs.yml", parameters={
        "runProfile": "v8",
        "runtimeBranch": "release/11.0",
        "downloadSpecificBuild": {"buildId": "test-only", "branchName": "refs/pull/123/merge"},
    })
    assert {run["runtimetype"] for run in run_parameters(cached_pr)} == {"wasm"}


def test_current_non_mono_coverage_and_wasm_artifact_contract():
    desktop = jobs("runtime-perf-jobs.yml")
    builder = next(job for job in desktop if "perf-coreclr-build-jobs.yml@" in job.get("template", ""))
    assert builder["parameters"] == dict.fromkeys([
        "linux_x64", "linux_arm64", "windows_x64", "windows_x86",
        "android_arm64", "coreclr_r2r_interpreter",
    ], True)
    assert {"AndroidCoreCLR", "iOSCoreCLR", "iOSNativeAOT"} <= {
        run.get("runtimetype") for run in run_parameters(desktop)
    }

    wasm = jobs("runtime-wasm-perf-jobs.yml", parameters={"runProfile": "v8"})
    runs = run_parameters(wasm)
    assert len(runs) == 2
    assert {run.get("r2rruntype", "") for run in runs} == {"", "r2r"}
    assert {run["runtimetype"] for run in runs} == {"wasm_coreclr"}
    builder = next(job["parameters"] for job in wasm
                   if job.get("parameters", {}).get("runtimeFlavor") == "coreclr"
                   and job["parameters"].get("platforms") == ["browser_wasm"])
    assert builder["buildConfig"] == "Release"
    build = builder["jobParameters"]
    assert build["nameSuffix"] == "wasm_coreclr"
    assert "mono+" not in build["buildArgs"]
    assert "/p:BuildHostTools=true /p:CrossBuildHostTools=true" in build["buildArgs"]
    assert not build.get("dependsOn")
    artifact = build["postBuildSteps"][0]["parameters"]
    assert artifact["runtimeFlavor"] == "coreclr"
    assert artifact["artifactName"] == "BrowserWasmCoreCLR"
    assert artifact["sdkDirName"] == "dotnet-none"
    assert artifact["includeCoreClrToolchainPacks"] is True
    assert artifact["includeRefPack"] is False
    assert jobs("runtime-wasm-perf-jobs.yml", parameters={"runProfile": "non-v8"}) == []


@pytest.mark.parametrize("branch, expected", [
    ("refs/heads/main", CURRENT_BUILDS),
    ("refs/heads/release/11.0", CURRENT_BUILDS | MONO_BUILDS),
])
def test_registration_upload_and_build_selection_agree(branch, expected):
    parameters = {"buildType": sorted(MONO_BUILDS | CURRENT_BUILDS), "runtimeBranch": branch}
    registered = jobs("register-build-jobs.yml", parameters=parameters)
    uploaded = jobs("upload-build-artifacts-jobs.yml", parameters=parameters)
    assert {job["parameters"]["buildType"] for job in registered} == expected
    assert {job["parameters"]["buildType"] for job in uploaded} == expected
    built = jobs("runtime-perf-build-jobs.yml", parameters=parameters)
    affected_uploads = [job for job in uploaded
                        if job["parameters"]["buildType"] in MONO_BUILDS | {"wasm_coreclr"}]
    assert {job["parameters"]["dependencyJobName"].lower() for job in affected_uploads} <= produced_jobs(built)
    mono = [job for job in built if "perf-mono-build-jobs.yml@" in job.get("template", "")]
    assert len(mono) == (4 if MONO_BUILDS <= expected else 0)
    wasm_upload = next(job["parameters"] for job in uploaded
                       if job["parameters"]["buildType"] == "wasm_coreclr")
    assert wasm_upload["dependencyJobName"] == "build_browser_wasm_linux_Release_wasm_coreclr"
    assert wasm_upload["artifacts"] == [
        {"artifactName": "BrowserWasmCoreCLR", "files": ["BrowserWasmCoreCLR.tar.gz"]}
    ]


def test_removed_build_types_do_not_build_coreclr_as_a_side_effect():
    assert jobs("runtime-perf-build-jobs.yml", parameters={"buildType": sorted(MONO_BUILDS)}) == []


@pytest.mark.parametrize("branch, runtime_type, artifact", [
    ("refs/heads/main", "wasm_coreclr", "BrowserWasmCoreCLR"),
    ("refs/heads/release/11.0", "wasm", "BrowserWasm"),
])
def test_selected_wasm_consumers_depend_on_matching_producer(branch, runtime_type, artifact):
    selected = jobs("runtime-wasm-perf-jobs.yml", branch, {"runProfile": "v8"})
    runs = [run for run in run_parameters(selected) if run["runtimetype"] == runtime_type]
    assert runs
    for run in runs:
        # The runtime platform matrix forwards these parameters to runtime-perf-job.
        consumer = jobs("templates/runtime-perf-job.yml", branch, {
            "runtimeType": runtime_type, "codeGenType": run["codegentype"],
            "buildConfig": "Release", "osGroup": "linux", "archType": "x64",
            "r2rRunType": run.get("r2rruntype", ""),
        })[0]["parameters"]
        assert {name.lower() for name in consumer["dependsOn"]} <= produced_jobs(selected)
        downloads = [
            step["parameters"]["artifactName"] for step in consumer["steps"]
            if "download-artifact-step.yml" in step.get("template", "")
        ]
        assert artifact in downloads
        if runtime_type == "wasm_coreclr":
            assert downloads == [artifact]


def test_legacy_pr_wasm_aot_exclusion_is_preserved():
    selected = jobs("runtime-wasm-perf-jobs.yml", "refs/pull/123/merge", {
        "runProfile": "v8",
    }, {"System.PullRequest.TargetBranch": "refs/heads/release/11.0",
        "Build.DefinitionName": "runtime-wasm-perf"})
    assert {run["codegentype"] for run in run_parameters(selected)} == {"wasm"}


def test_sdk_mono_baselines_keep_explicit_channels_and_schedules():
    expanded = jobs("sdk-perf-jobs.yml", parameters={
        "runPublicJobs": True, "runPrivateJobs": True, "runScheduledPrivateJobs": True,
    })
    configurations = [job["parameters"]["jobParameters"] for job in expanded
                      if "jobParameters" in job.get("parameters", {})]
    blazor = [config for config in configurations if config["runKind"] == "blazor_scenarios"]
    assert len(blazor) == 2
    for config in blazor:
        assert config["channels"] == [11.0, 9.0, 8.0]
        assert config["additionalJobIdentifier"] == "MonoBaseline"
        assert config["runtimeFlavor"] == "mono"
    assert ChannelMap.get_branch("11.0") == "11.0"
    assert ChannelMap.get_target_framework_moniker("11.0") == "net11.0"
    assert ChannelMap.get_quality_from_channel("11.0") == "daily"
    maui = [config for config in configurations if config["runKind"].startswith("maui_")]
    mono = [config for config in maui if config["runtimeFlavor"] == "mono"]
    assert len(mono) == 2
    assert all(config["channels"] == [10.0] for config in mono)
    assert all(config["variables"] == [{"name": "MAUI_WORKLOAD_MODE", "value": "stable"}]
               for config in mono)
    assert all(config["channels"] == ["main"] for config in maui
               if config["runtimeFlavor"] == "coreclr")

    entrypoint = yaml.safe_load((ROOT / "azure-pipelines.yml").read_text(encoding="utf-8"))
    schedule = next(item for item in entrypoint["schedules"]
                    if item["displayName"] == "Every 12 hours build")
    assert schedule["cron"] == "0 */12 * * *"
    assert schedule["branches"]["include"] == ["main"]
    scheduled = expand(entrypoint["jobs"][0], {
        "parameters": dict.fromkeys([
            "runPublicJobs", "runPrivateJobs", "runScheduledPrivateJobs", "onlySanityCheck",
        ], False),
        "variables": {"System.TeamProject": "internal", "Build.Reason": "Schedule",
                      "Build.CronSchedule.DisplayName": "Every 12 hours build"},
    })
    assert scheduled["parameters"]["runScheduledPrivateJobs"] is True


@pytest.mark.parametrize("flavor", ["mono", "coreclr"])
def test_blazor_result_runtime_matches_selected_baseline(flavor):
    config = get_run_configurations(
        "blazor_scenarios", runtime_type="", codegen_type="", runtime_flavor=flavor,
    )
    assert config["RuntimeType"] == flavor


def test_blazor_unset_runtime_preserves_existing_configuration():
    config = get_run_configurations("blazor_scenarios", runtime_type="", codegen_type="")
    assert "RuntimeType" not in config
    with pytest.raises(Exception, match="Runtime flavor must be mono or coreclr"):
        get_run_configurations(
            "blazor_scenarios", runtime_type="", codegen_type="", runtime_flavor="invalid",
        )
