"""Tests for the ConfigMap-safe rollout rehearsal.

Covers both the planning core (app.rehearsal) and the HTTP endpoint, using the
bundled payment-web v3 chart and the hand-authored "live" manifests under
live-manifests/.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from app.rehearsal import (
    CONTENT_HASH_ANNOTATION,
    RehearsalError,
    RehearsalRequestError,
    collect_references,
    configmap_content,
    copy_name_for,
    plan_rollout,
)

ROOT = Path("/workspace")
LIVE = ROOT / "live-manifests" / "payment-web"
CHART_V3 = ROOT / "mounted-charts" / "payment-web" / "v3"
VALUES = ROOT / "rehearsal-values" / "payment-web.rehearsal.values.yaml"
RELEASE = "platform-rel"
NAMESPACE = "platform"
CM_NAME = "platform-rel-payment-web-config"
EXTERNAL_NAME = "platform-shared-config"
OPTIONAL_NAME = "payment-web-optional-flags"

pytestmark = pytest.mark.skipif(not LIVE.is_dir(), reason="rehearsal fixtures not present")


@pytest.fixture
def live_deployment() -> dict:
    return yaml.safe_load((LIVE / "deployment.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def live_configmaps() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(
            (LIVE / "configmaps.yaml").read_text(encoding="utf-8")
        )
        if doc is not None
    ]


@pytest.fixture
def conflict_configmaps() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(
            (LIVE / "configmaps.conflict.yaml").read_text(encoding="utf-8")
        )
        if doc is not None
    ]


def _plan(deployment, configmaps):
    return plan_rollout(
        current_deployment=deployment,
        live_configmaps=configmaps,
        chart_dir=CHART_V3,
        values_file=VALUES,
        release_name=RELEASE,
        namespace=NAMESPACE,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_happy_path_creates_one_immutable_copy_and_keeps_external_ref(
    live_deployment, live_configmaps
):
    result = _plan(live_deployment, live_configmaps)

    assert result.deployment_name == "platform-rel-payment-web"
    assert result.rendered_documents >= 3
    plans = {p.source_name: p for p in result.configmap_plans}

    # Chart-managed CM changed -> immutable copy, freshly created.
    changed = plans[CM_NAME]
    assert changed.immutable is True
    assert changed.reused is False
    assert changed.copy_name != CM_NAME
    assert len(result.configmaps_to_create) == 1

    copy_cm = result.configmaps_to_create[0]
    assert copy_cm["immutable"] is True
    assert copy_cm["metadata"]["name"] == changed.copy_name
    assert copy_cm["metadata"]["namespace"] == NAMESPACE
    assert copy_cm["metadata"]["annotations"][CONTENT_HASH_ANNOTATION]
    assert configmap_content(copy_cm)["data"]["featureToggle"] == "enabled"

    # External CM is kept on its original name, no copy created for it.
    external = plans[EXTERNAL_NAME]
    assert external.reused is True
    assert external.copy_name == EXTERNAL_NAME


def test_references_rewritten_in_all_three_supported_locations(
    live_deployment, live_configmaps
):
    result = _plan(live_deployment, live_configmaps)
    copy_name = result.configmaps_to_create[0]["metadata"]["name"]

    kinds_locations = {(c.kind, c.location.split(".")[0].split("(")[0]) for c in result.reference_changes}
    assert ("envFrom", "container payment-web") in kinds_locations
    assert ("envFrom", "container config-bootstrap") in kinds_locations
    assert ("volume", "volumes[0]") in kinds_locations
    assert all(c.target_name == copy_name for c in result.reference_changes)

    pod_spec = result.new_deployment["spec"]["template"]["spec"]
    # External reference untouched.
    env_from = pod_spec["containers"][0]["envFrom"]
    assert env_from[1]["configMapRef"]["name"] == EXTERNAL_NAME
    # Optional references (envFrom + volume) left exactly as rendered.
    assert env_from[2]["configMapRef"]["name"] == OPTIONAL_NAME
    assert env_from[2]["configMapRef"]["optional"] is True
    assert pod_spec["volumes"][1]["configMap"]["name"] == OPTIONAL_NAME
    assert pod_spec["volumes"][1]["configMap"]["optional"] is True
    # Mandatory volume now points at the copy.
    assert pod_spec["volumes"][0]["configMap"]["name"] == copy_name


def test_optional_references_are_listed_as_skipped(live_deployment, live_configmaps):
    result = _plan(live_deployment, live_configmaps)
    joined = "\n".join(result.skipped_optional_references)
    assert OPTIONAL_NAME in joined
    assert len(result.skipped_optional_references) == 2


def test_rollback_deployment_is_original_and_apply_ready(live_deployment, live_configmaps):
    result = _plan(live_deployment, live_configmaps)
    rollback = result.rollback_deployment

    assert rollback["kind"] == "Deployment"
    assert rollback["metadata"]["name"] == result.deployment_name
    # Runtime/cluster-assigned metadata stripped.
    assert "resourceVersion" not in rollback["metadata"]
    assert "uid" not in rollback["metadata"]
    assert "generation" not in rollback["metadata"]
    # Rollback keeps the ORIGINAL config names.
    pod_spec = rollback["spec"]["template"]["spec"]
    assert pod_spec["volumes"][0]["configMap"]["name"] == CM_NAME
    assert pod_spec["containers"][0]["envFrom"][0]["configMapRef"]["name"] == CM_NAME
    # The live input itself was never mutated.
    assert "resourceVersion" in live_deployment["metadata"]


def test_live_configmaps_are_never_mutated(live_deployment, live_configmaps):
    before = copy.deepcopy(live_configmaps)
    _plan(live_deployment, live_configmaps)
    assert live_configmaps == before


def test_selector_must_stay_identical(live_deployment, live_configmaps):
    broken = copy.deepcopy(live_deployment)
    broken["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] = "renamed"
    with pytest.raises(RehearsalError) as exc:
        _plan(broken, live_configmaps)
    assert any("selector" in r for r in exc.value.reasons)


def test_namespace_mismatch_is_rejected(live_deployment, live_configmaps):
    broken = copy.deepcopy(live_deployment)
    broken["metadata"]["namespace"] = "other-ns"
    with pytest.raises(RehearsalError) as exc:
        _plan(broken, live_configmaps)
    assert any("namespace" in r for r in exc.value.reasons)


def test_rendered_deployment_name_must_match(live_deployment, live_configmaps):
    broken = copy.deepcopy(live_deployment)
    broken["metadata"]["name"] = "totally-different-deploy"
    with pytest.raises(RehearsalError) as exc:
        _plan(broken, live_configmaps)
    assert any("no rendered Deployment" in r for r in exc.value.reasons)


def test_missing_config_for_new_reference_is_rejected(live_deployment, live_configmaps):
    without_external = [
        cm for cm in live_configmaps if cm["metadata"]["name"] != EXTERNAL_NAME
    ]
    with pytest.raises(RehearsalError) as exc:
        _plan(live_deployment, without_external)
    assert any(EXTERNAL_NAME in r and "missing ConfigMap" in r for r in exc.value.reasons)


def test_missing_config_for_current_reference_is_rejected(live_deployment, live_configmaps):
    without_chart_cm = [
        cm for cm in live_configmaps if cm["metadata"]["name"] != CM_NAME
    ]
    with pytest.raises(RehearsalError) as exc:
        _plan(live_deployment, without_chart_cm)
    assert any("current Deployment" in r for r in exc.value.reasons)


def test_missing_volume_key_on_live_configmap_is_rejected(live_deployment, live_configmaps):
    broken_cms = copy.deepcopy(live_configmaps)
    del broken_cms[0]["data"]["cacheTtl"]
    with pytest.raises(RehearsalError) as exc:
        _plan(live_deployment, broken_cms)
    assert any("missing key" in r and "cacheTtl" in r for r in exc.value.reasons)


def test_missing_volume_key_on_rendered_configmap_is_rejected(
    live_deployment, live_configmaps, tmp_path
):
    # Rewrite the chart values into a temp chart variant whose rendered CM
    # drops a key that the rendered volume explicitly requests.
    import shutil

    variant = tmp_path / "v3-broken"
    shutil.copytree(CHART_V3, variant)
    cm_template = variant / "templates" / "configmap.yaml"
    text = cm_template.read_text(encoding="utf-8").replace(
        'cacheTtl: {{ .Values.config.cacheTtl | quote }}',
        'cacheTtlRemoved: "x"',
    )
    cm_template.write_text(text, encoding="utf-8")

    with pytest.raises(RehearsalError) as exc:
        plan_rollout(
            current_deployment=live_deployment,
            live_configmaps=live_configmaps,
            chart_dir=variant,
            values_file=VALUES,
            release_name=RELEASE,
            namespace=NAMESPACE,
        )
    assert any("cacheTtl" in r and "missing key" in r for r in exc.value.reasons)


def test_name_conflict_when_copy_name_occupied_by_mutable_object(
    live_deployment, conflict_configmaps
):
    with pytest.raises(RehearsalError) as exc:
        _plan(live_deployment, conflict_configmaps)
    assert any("name conflict" in r and "not marked immutable" in r for r in exc.value.reasons)


def test_name_conflict_when_immutable_occupant_has_different_content(
    live_deployment, live_configmaps
):
    result = _plan(live_deployment, live_configmaps)
    cms = copy.deepcopy(live_configmaps)
    bad_copy = copy.deepcopy(result.configmaps_to_create[0])
    bad_copy["data"]["featureToggle"] = "disabled"
    cms.append(bad_copy)
    with pytest.raises(RehearsalError) as exc:
        _plan(live_deployment, cms)
    assert any("name conflict" in r and "different content" in r for r in exc.value.reasons)


def test_existing_immutable_same_content_copy_is_reused(live_deployment, live_configmaps):
    first = _plan(live_deployment, live_configmaps)
    copy_name = first.configmaps_to_create[0]["metadata"]["name"]

    # Simulate the copy having been applied on the first attempt.
    cms = copy.deepcopy(live_configmaps)
    cms.append(copy.deepcopy(first.configmaps_to_create[0]))
    second = _plan(live_deployment, cms)

    plan = next(p for p in second.configmap_plans if p.source_name == CM_NAME)
    assert plan.reused is True
    assert plan.copy_name == copy_name
    assert second.configmaps_to_create == []
    # The rewritten target is still the same name -> retry is stable.
    assert all(c.target_name == copy_name for c in second.reference_changes)


def test_copy_name_is_deterministic_across_retries(live_deployment, live_configmaps):
    first = _plan(live_deployment, live_configmaps)
    second = _plan(copy.deepcopy(live_deployment), copy.deepcopy(live_configmaps))
    n1 = [m["metadata"]["name"] for m in first.configmaps_to_create]
    n2 = [m["metadata"]["name"] for m in second.configmaps_to_create]
    assert n1 == n2 and n1


def test_unchanged_same_name_configmap_keeps_original_name(live_deployment, live_configmaps):
    cms = copy.deepcopy(live_configmaps)
    cms[0]["data"] = {
        "cacheTtl": "30",
        "apiBase": "https://api-v3.example.com",
        "featureToggle": "enabled",
    }
    result = _plan(live_deployment, cms)
    plan = next(p for p in result.configmap_plans if p.source_name == CM_NAME)
    assert plan.reused is True
    assert plan.copy_name == CM_NAME
    assert result.configmaps_to_create == []
    assert result.reference_changes == []


def test_copy_name_respects_rfc1123_length_limit():
    name = copy_name_for("x" * 100, "0" * 64)
    assert len(name) <= 63
    assert name.endswith("-" + "0" * 12)


def test_collect_references_ignores_secret_refs(live_deployment):
    refs = collect_references(live_deployment)
    assert refs
    assert all(r.kind in {"envFrom", "volume"} for r in refs)
    # Optional refs are discovered but flagged.
    assert any(r.optional for r in refs)


# --------------------------------------------------------------------------- #
# HTTP endpoint
# --------------------------------------------------------------------------- #

client = TestClient(app)


def _payload(configmap_file: str = "configmaps.yaml") -> dict:
    return {
        "release_name": RELEASE,
        "namespace": NAMESPACE,
        "chart_dir": str(CHART_V3),
        "values_path": str(VALUES),
        "current_deployment": (LIVE / "deployment.yaml").read_text(encoding="utf-8"),
        "current_configmaps": (LIVE / configmap_file).read_text(encoding="utf-8"),
    }


def test_openapi_ships_two_rehearsal_examples():
    schema = app.openapi()
    examples = schema["paths"]["/api/v1/rehearse-rollout"]["post"]["requestBody"][
        "content"
    ]["application/json"]["examples"]
    assert len(examples) == 2
    assert any("happy path" in name for name in examples)
    assert any("conflict" in name for name in examples)


def test_endpoint_happy_path_orders_documents_configmap_new_rollback():
    resp = client.post("/api/v1/rehearse-rollout", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    docs = [yaml.safe_load(chunk) for chunk in body["release_yaml"].split("---") if chunk.strip()]
    assert [d["kind"] for d in docs] == ["ConfigMap", "Deployment", "Deployment"]
    assert docs[1]["metadata"]["name"] == docs[2]["metadata"]["name"]
    assert body["configmaps_to_create"][0]["immutable"] is True


def test_endpoint_accepts_object_inputs_too():
    payload = _payload()
    payload["current_deployment"] = yaml.safe_load(payload["current_deployment"])
    payload["current_configmaps"] = [
        d for d in yaml.safe_load_all(payload["current_configmaps"]) if d
    ]
    resp = client.post("/api/v1/rehearse-rollout", json=payload)
    assert resp.status_code == 200


def test_endpoint_conflict_is_422_with_reasons_and_no_manifests():
    resp = client.post(
        "/api/v1/rehearse-rollout", json=_payload("configmaps.conflict.yaml")
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any("name conflict" in item for item in detail)
    # Error responses never carry release manifests.
    assert "release_yaml" not in resp.json()


def test_endpoint_missing_config_is_422():
    payload = _payload()
    cms = [d for d in yaml.safe_load_all(payload["current_configmaps"]) if d]
    payload["current_configmaps"] = [
        cm for cm in cms if cm["metadata"]["name"] != EXTERNAL_NAME
    ]
    resp = client.post("/api/v1/rehearse-rollout", json=payload)
    assert resp.status_code == 422
    assert any("missing ConfigMap" in item for item in resp.json()["detail"])


def test_endpoint_bad_yaml_is_400():
    payload = _payload()
    payload["current_deployment"] = "metadata: [unterminated"
    resp = client.post("/api/v1/rehearse-rollout", json=payload)
    assert resp.status_code == 400


def test_endpoint_missing_chart_dir_is_400():
    payload = _payload()
    payload["chart_dir"] = "/workspace/mounted-charts/nope/v9"
    resp = client.post("/api/v1/rehearse-rollout", json=payload)
    assert resp.status_code == 400


def test_endpoint_supports_yaml_accept():
    resp = client.post(
        "/api/v1/rehearse-rollout",
        json=_payload(),
        headers={"Accept": "application/yaml"},
    )
    assert resp.status_code == 200
    assert "application/yaml" in resp.headers["content-type"]
    assert "release_yaml:" in resp.text
