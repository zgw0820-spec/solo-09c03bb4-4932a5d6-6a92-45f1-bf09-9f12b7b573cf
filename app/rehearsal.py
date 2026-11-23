"""Rollout rehearsal: plan an in-place ConfigMap-safe Deployment upgrade.

Problem: a rolling update must never let an old Pod read the *new* version's
configuration. Kubernetes does not version ConfigMaps, so mutating an in-use
ConfigMap in place changes what both old and new Pods see. The safe pattern is:

* never touch the live ConfigMaps;
* for every ConfigMap whose content changes (or which does not exist yet),
  create a new **immutable** copy under a content-derived name;
* point the new Deployment at the copies (and keep references to unchanged
  ConfigMaps on their original names);
* keep a rollback Deployment that references the original names.

This module only *plans*. It never connects to a cluster: the caller supplies
the live Deployment and the live ConfigMaps of the same namespace, plus a
chart directory + values file for the new version. The chart is rendered with
``helm template`` and only the rendered Deployment that keeps the same name,
namespace and label selector is processed.

Supported in-scope references (optional references are intentionally left
alone, because an optional reference does not have to resolve at apply time):

* ``.spec.template.spec.containers[*].envFrom[*].configMapRef``
* ``.spec.template.spec.initContainers[*].envFrom[*].configMapRef``
  (non-optional only; ``secretRef`` is out of scope)
* ``.spec.template.spec.volumes[*].configMap`` (non-optional only)

References to ConfigMaps that the chart does not render ("external"
ConfigMaps, e.g. shared platform config) are kept on their original names and
only validated against the submitted live ConfigMaps.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .helm import HelmRenderError, HelmResult, helm_template
from .normalize import parse_documents

IMMUTABLE_COPY_ANNOTATION = "rehearsal.platform/source-configmap"
CONTENT_HASH_ANNOTATION = "rehearsal.platform/content-hash"
MAX_NAME_LENGTH = 63


class RehearsalError(Exception):
    """A rehearsal planning failure.

    ``reasons`` carries every problem detected in one pass so the caller can
    report all of them instead of one error per request.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class RehearsalRequestError(RehearsalError):
    """Malformed request / unreadable inputs (maps to HTTP 400)."""


@dataclass
class ConfigMapPlan:
    source_name: str
    copy_name: str
    immutable: bool
    reused: bool
    reason: str
    manifest: dict[str, Any] | None = None


@dataclass
class ReferenceChange:
    kind: str  # "envFrom" | "volume"
    location: str  # e.g. "container orders-api.envFrom[0]" or "volumes[2]"
    source_name: str
    target_name: str
    rewritten: bool


@dataclass
class RehearsalResult:
    release_name: str
    namespace: str
    deployment_name: str
    configmap_plans: list[ConfigMapPlan]
    reference_changes: list[ReferenceChange]
    configmaps_to_create: list[dict[str, Any]]
    new_deployment: dict[str, Any]
    rollback_deployment: dict[str, Any]
    skipped_optional_references: list[str] = field(default_factory=list)
    rendered_documents: int = 0


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #

def parse_manifest_field(value: Any, field_name: str) -> list[dict[str, Any]]:
    """Accept a mapping, a list of mappings or a multi-document YAML string."""
    if value is None:
        raise RehearsalError([f"{field_name} is required"])
    if isinstance(value, dict):
        docs = [value]
    elif isinstance(value, list):
        docs = value
    elif isinstance(value, str):
        try:
            docs = parse_documents(value)
        except yaml.YAMLError as exc:
            raise RehearsalRequestError([f"{field_name} is not valid YAML: {exc}"]) from exc
    else:
        raise RehearsalRequestError(
            [f"{field_name} must be an object, a list of objects or a YAML string"]
        )
    non_mappings = [i for i, doc in enumerate(docs) if not isinstance(doc, dict)]
    if non_mappings:
        raise RehearsalRequestError(
            [f"{field_name}[{i}] is not a YAML mapping" for i in non_mappings]
        )
    return docs


def _meta(doc: dict[str, Any]) -> dict[str, Any]:
    meta = doc.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _name(doc: dict[str, Any]) -> str:
    return str(_meta(doc).get("name") or "")


def _namespace_of(doc: dict[str, Any]) -> str:
    return str(_meta(doc).get("namespace") or "")


def _selector(deployment: dict[str, Any]) -> Any:
    return (deployment.get("spec") or {}).get("selector")


# --------------------------------------------------------------------------- #
# Reference discovery
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CmReference:
    kind: str  # "envFrom" | "volume"
    location: str
    name: str
    optional: bool
    # Path of the reference node inside the pod spec, used for rewriting.
    path: tuple[Any, ...]


def _env_from_refs(containers: Any, pod_spec: dict[str, Any]) -> list[CmReference]:
    refs: list[CmReference] = []
    if not isinstance(containers, list):
        return refs
    for c_index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        c_name = container.get("name", f"[{c_index}]")
        env_from = container.get("envFrom")
        if not isinstance(env_from, list):
            continue
        for e_index, entry in enumerate(env_from):
            if not isinstance(entry, dict) or "configMapRef" not in entry:
                continue  # secretRef (or malformed) is out of scope
            ref = entry["configMapRef"]
            if not isinstance(ref, dict) or not ref.get("name"):
                continue
            refs.append(
                CmReference(
                    kind="envFrom",
                    location=f"container {c_name}.envFrom[{e_index}]",
                    name=str(ref["name"]),
                    optional=bool(ref.get("optional", False)),
                    path=("containers", c_index, "envFrom", e_index, "configMapRef"),
                )
            )
    return refs


def _volume_refs(pod_spec: dict[str, Any]) -> list[CmReference]:
    refs: list[CmReference] = []
    volumes = pod_spec.get("volumes")
    if not isinstance(volumes, list):
        return refs
    for v_index, volume in enumerate(volumes):
        if not isinstance(volume, dict) or "configMap" not in volume:
            continue
        cm_volume = volume["configMap"]
        if not isinstance(cm_volume, dict) or not cm_volume.get("name"):
            continue
        v_name = volume.get("name", f"[{v_index}]")
        refs.append(
            CmReference(
                kind="volume",
                location=f"volumes[{v_index}](name={v_name})",
                name=str(cm_volume["name"]),
                optional=bool(cm_volume.get("optional", False)),
                path=("volumes", v_index, "configMap"),
            )
        )
    return refs


def collect_references(deployment: dict[str, Any]) -> list[CmReference]:
    """All ConfigMap references supported by the rehearsal."""
    pod_spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec")
    if not isinstance(pod_spec, dict):
        return []
    refs = _env_from_refs(pod_spec.get("containers"), pod_spec)
    refs.extend(_env_from_refs(pod_spec.get("initContainers"), pod_spec))
    refs.extend(_volume_refs(pod_spec))
    return refs


def _walk(node: Any, path: tuple[Any, ...]) -> Any:
    cur = node
    for segment in path:
        cur = cur[segment]
    return cur


def rewrite_references(
    deployment: dict[str, Any],
    rename: dict[str, str],
) -> list[ReferenceChange]:
    """Rewrite in-scope reference names in place; return what was changed."""
    changes: list[ReferenceChange] = []
    pod_spec = deployment["spec"]["template"]["spec"]
    for ref in collect_references(deployment):
        if ref.optional or ref.name not in rename or rename[ref.name] == ref.name:
            continue
        target = _walk(pod_spec, ref.path)
        target["name"] = rename[ref.name]
        changes.append(
            ReferenceChange(
                kind=ref.kind,
                location=ref.location,
                source_name=ref.name,
                target_name=rename[ref.name],
                rewritten=True,
            )
        )
    return changes


# --------------------------------------------------------------------------- #
# ConfigMap content handling
# --------------------------------------------------------------------------- #

def configmap_content(doc: dict[str, Any]) -> dict[str, Any]:
    """The content that determines a copy: data + binaryData, sorted keys."""
    content: dict[str, Any] = {}
    data = doc.get("data")
    if isinstance(data, dict) and data:
        content["data"] = {str(k): str(v) for k, v in data.items()}
    binary = doc.get("binaryData")
    if isinstance(binary, dict) and binary:
        content["binaryData"] = {str(k): str(v) for k, v in binary.items()}
    return content


def content_hash(content: dict[str, Any]) -> str:
    blob = yaml.safe_dump(content, sort_keys=True, width=1000)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def copy_name_for(source_name: str, digest: str) -> str:
    """Deterministic, retry-stable immutable copy name (RFC 1123 safe)."""
    suffix = digest[:12]
    base = source_name
    budget = MAX_NAME_LENGTH - len(suffix) - 1
    if len(base) > budget:
        base = base[:budget].rstrip("-")
    return f"{base}-{suffix}"


def build_immutable_copy(
    source: dict[str, Any],
    copy_name: str,
    namespace: str,
    digest: str,
) -> dict[str, Any]:
    """Project a rendered/live ConfigMap onto an immutable copy manifest."""
    meta = _meta(source)
    copy_meta: dict[str, Any] = {"name": copy_name, "namespace": namespace}
    labels = meta.get("labels")
    if isinstance(labels, dict) and labels:
        copy_meta["labels"] = copy.deepcopy(labels)
    annotations = meta.get("annotations")
    copy_annotations: dict[str, Any] = {}
    if isinstance(annotations, dict):
        copy_annotations.update(copy.deepcopy(annotations))
    copy_annotations[IMMUTABLE_COPY_ANNOTATION] = _name(source)
    copy_annotations[CONTENT_HASH_ANNOTATION] = digest
    copy_meta["annotations"] = copy_annotations

    manifest: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": copy_meta,
        "immutable": True,
    }
    if isinstance(source.get("data"), dict):
        manifest["data"] = copy.deepcopy(source["data"])
    if isinstance(source.get("binaryData"), dict):
        manifest["binaryData"] = copy.deepcopy(source["binaryData"])
    return manifest


def _strip_runtime_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop cluster-assigned fields so the rollback manifest is apply-able."""
    out = copy.deepcopy(doc)
    meta = out.get("metadata")
    if isinstance(meta, dict):
        for key in (
            "uid",
            "resourceVersion",
            "generation",
            "creationTimestamp",
            "managedFields",
            "ownerReferences",
        ):
            meta.pop(key, None)
    out.pop("status", None)
    return out


def volume_required_keys(volume_ref_node: dict[str, Any]) -> list[str]:
    """Keys explicitly requested by a configMap volume (items[].key)."""
    items = volume_ref_node.get("items")
    if not isinstance(items, list):
        return []
    return [str(item["key"]) for item in items if isinstance(item, dict) and "key" in item]


def _cm_keys(doc: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("data", "binaryData"):
        section = doc.get(field)
        if isinstance(section, dict):
            keys.update(str(k) for k in section)
    return keys


# --------------------------------------------------------------------------- #
# Main planning pipeline
# --------------------------------------------------------------------------- #

def plan_rollout(
    *,
    current_deployment: dict[str, Any],
    live_configmaps: list[dict[str, Any]],
    chart_dir: Path,
    values_file: Path | None,
    release_name: str,
    namespace: str,
) -> RehearsalResult:
    reasons: list[str] = []

    # --- validate the live Deployment -------------------------------------
    if current_deployment.get("kind") != "Deployment":
        raise RehearsalError(
            [f"current_deployment must be kind=Deployment, got {current_deployment.get('kind')!r}"]
        )
    dep_name = _name(current_deployment)
    if not dep_name:
        reasons.append("current_deployment.metadata.name is required")
    dep_ns = _namespace_of(current_deployment)
    if dep_ns and dep_ns != namespace:
        reasons.append(
            f"current_deployment namespace {dep_ns!r} does not match request namespace {namespace!r}"
        )
    current_selector = _selector(current_deployment)
    if current_selector is None:
        reasons.append("current_deployment.spec.selector is required")

    # --- validate the live ConfigMaps --------------------------------------
    live_by_name: dict[str, dict[str, Any]] = {}
    for index, cm in enumerate(live_configmaps):
        if cm.get("kind") != "ConfigMap":
            reasons.append(
                f"current_configmaps[{index}] must be kind=ConfigMap, got {cm.get('kind')!r}"
            )
            continue
        name = _name(cm)
        if not name:
            reasons.append(f"current_configmaps[{index}].metadata.name is required")
            continue
        cm_ns = _namespace_of(cm)
        if cm_ns and cm_ns != namespace:
            reasons.append(
                f"ConfigMap {name!r} namespace {cm_ns!r} does not match request namespace {namespace!r}"
            )
        if name in live_by_name:
            reasons.append(f"duplicate live ConfigMap submitted: {name}")
            continue
        live_by_name[name] = cm

    if reasons:
        # Everything detected before rendering is input validation.
        raise RehearsalRequestError(reasons)

    # --- render the new chart ----------------------------------------------
    if not chart_dir.is_dir():
        raise RehearsalRequestError([f"chart directory not found: {chart_dir}"])
    if values_file is not None and not values_file.is_file():
        raise RehearsalRequestError([f"values file not found: {values_file}"])
    try:
        rendered: HelmResult = helm_template(
            chart_dir, values_file, release_name, namespace
        )
    except (HelmRenderError, FileNotFoundError) as exc:
        raise RehearsalError([f"helm template failed: {exc}"]) from exc
    if rendered.returncode != 0:
        raise RehearsalError(
            [f"helm template failed (exit {rendered.returncode}): {rendered.stderr.strip()[:500]}"]
        )
    try:
        rendered_docs = parse_documents(rendered.stdout)
    except yaml.YAMLError as exc:
        raise RehearsalError([f"helm output is not valid YAML: {exc}"]) from exc

    rendered_deployments = [d for d in rendered_docs if d.get("kind") == "Deployment"]
    rendered_cms = [d for d in rendered_docs if d.get("kind") == "ConfigMap"]
    rendered_cm_by_name: dict[str, dict[str, Any]] = {}
    for cm in rendered_cms:
        name = _name(cm)
        if name in rendered_cm_by_name:
            reasons.append(f"chart renders duplicate ConfigMap name: {name}")
        rendered_cm_by_name[name] = cm

    matches = [d for d in rendered_deployments if _name(d) == dep_name]
    if not matches:
        rendered_names = ", ".join(sorted(_name(d) for d in rendered_deployments)) or "<none>"
        reasons.append(
            f"no rendered Deployment keeps the current name {dep_name!r}; "
            f"rendered Deployments: {rendered_names}"
        )
        raise RehearsalError(reasons)
    if len(matches) > 1:
        reasons.append(f"chart renders multiple Deployments named {dep_name!r}")
        raise RehearsalError(reasons)
    new_deployment = copy.deepcopy(matches[0])

    new_ns = _namespace_of(new_deployment)
    if new_ns and new_ns != namespace:
        reasons.append(
            f"rendered Deployment namespace {new_ns!r} does not match request namespace {namespace!r}"
        )
    if _selector(new_deployment) != current_selector:
        reasons.append(
            "rendered Deployment changed spec.selector; only same-selector rollouts are supported "
            f"(current={yaml.safe_dump(current_selector, sort_keys=True).strip()}, "
            f"rendered={yaml.safe_dump(_selector(new_deployment), sort_keys=True).strip()})"
        )
    if reasons:
        raise RehearsalError(reasons)

    # --- reference validation on both old and new Deployments --------------
    current_refs = collect_references(current_deployment)
    new_refs = collect_references(new_deployment)

    skipped_optional = sorted(
        {f"{r.location} -> {r.name}" for r in current_refs + new_refs if r.optional}
    )
    mandatory_current = [r for r in current_refs if not r.optional]
    mandatory_new = [r for r in new_refs if not r.optional]

    # Old references must resolve against the submitted live ConfigMaps
    # ("新旧引用缺配置 ... 报原因").
    for ref in mandatory_current:
        if ref.name not in live_by_name:
            reasons.append(
                f"current Deployment {ref.location} references missing ConfigMap {ref.name!r}"
            )

    # New references must resolve to either a rendered or a live ConfigMap.
    for ref in mandatory_new:
        if ref.name not in rendered_cm_by_name and ref.name not in live_by_name:
            reasons.append(
                f"rendered Deployment {ref.location} references missing ConfigMap {ref.name!r} "
                "(found neither in the chart output nor in the submitted live ConfigMaps)"
            )

    # Volume items must name keys that exist in the target ConfigMap. The
    # current Deployment points at live ConfigMaps; the rendered one points at
    # rendered ConfigMaps when the chart provides them, live ones otherwise.
    def _check_volume_keys(refs: list[CmReference], source: str) -> None:
        pod_spec = (({"new": new_deployment, "cur": current_deployment}[source]
                     .get("spec") or {}).get("template") or {}).get("spec") or {}
        for ref in refs:
            if ref.kind != "volume":
                continue
            node = _walk(pod_spec, ref.path)
            required = volume_required_keys(node)
            if not required:
                continue
            if source == "cur":
                cm = live_by_name.get(ref.name)
            else:
                cm = rendered_cm_by_name.get(ref.name) or live_by_name.get(ref.name)
            if cm is None:
                continue  # missing-CM error already reported above
            available = _cm_keys(cm)
            missing = [k for k in required if k not in available]
            if missing:
                reasons.append(
                    f"{source} Deployment {ref.location} requires missing key(s) "
                    f"{missing} in ConfigMap {ref.name!r}"
                )

    _check_volume_keys(mandatory_current, "cur")
    _check_volume_keys(mandatory_new, "new")

    if reasons:
        raise RehearsalError(reasons)

    # --- plan immutable copies ---------------------------------------------
    # Only ConfigMaps the new Deployment actually references are candidates.
    referenced_names = sorted({r.name for r in mandatory_new})
    plans: list[ConfigMapPlan] = []
    rename: dict[str, str] = {}
    manifests_to_create: list[dict[str, Any]] = []

    for name in referenced_names:
        rendered_cm = rendered_cm_by_name.get(name)
        live_cm = live_by_name.get(name)

        if rendered_cm is None:
            # Chart-external reference: keep the original name, no copy.
            rename[name] = name
            plans.append(
                ConfigMapPlan(
                    source_name=name,
                    copy_name=name,
                    immutable=bool(live_cm.get("immutable", False)),
                    reused=True,
                    reason="external ConfigMap (not rendered by the chart); reference kept unchanged",
                )
            )
            continue

        desired_content = configmap_content(rendered_cm)
        digest = content_hash(desired_content)

        if live_cm is not None and configmap_content(live_cm) == desired_content:
            # Same name, same content: nothing to create, keep the name.
            rename[name] = name
            plans.append(
                ConfigMapPlan(
                    source_name=name,
                    copy_name=name,
                    immutable=bool(live_cm.get("immutable", False)),
                    reused=True,
                    reason="live ConfigMap content already matches the rendered content; name kept",
                )
            )
            continue

        # Content differs (or the ConfigMap is new): an immutable copy is
        # required. The name is derived purely from content, so retries and
        # repeated requests always land on the same name.
        copy_name = copy_name_for(name, digest)
        occupant = live_by_name.get(copy_name)
        if occupant is not None:
            if occupant.get("immutable") is True and configmap_content(occupant) == desired_content:
                rename[name] = copy_name
                plans.append(
                    ConfigMapPlan(
                        source_name=name,
                        copy_name=copy_name,
                        immutable=True,
                        reused=True,
                        reason="existing same-name immutable copy has identical content; reused",
                    )
                )
                continue
            state = "immutable but different content" if occupant.get("immutable") is True \
                else "not marked immutable"
            reasons.append(
                f"name conflict for immutable copy {copy_name!r} (source {name!r}): "
                f"a live ConfigMap with that name already exists and is {state}"
            )
            continue

        # Also reject collisions between two copies planned in this request.
        if copy_name in {p.copy_name for p in plans}:
            reasons.append(
                f"name conflict: immutable copy name {copy_name!r} is required by more than one source"
            )
            continue

        manifest = build_immutable_copy(rendered_cm, copy_name, namespace, digest)
        rename[name] = copy_name
        manifests_to_create.append(manifest)
        plans.append(
            ConfigMapPlan(
                source_name=name,
                copy_name=copy_name,
                immutable=True,
                reused=False,
                reason=(
                    "new ConfigMap: no same-name live object"
                    if live_cm is None
                    else "content changed vs the live ConfigMap; the live object is left untouched"
                ),
                manifest=manifest,
            )
        )

    if reasons:
        raise RehearsalError(reasons)

    # --- rewrite references and assemble outputs ---------------------------
    changes = rewrite_references(new_deployment, rename)
    rollback_deployment = _strip_runtime_metadata(current_deployment)

    return RehearsalResult(
        release_name=release_name,
        namespace=namespace,
        deployment_name=dep_name,
        configmap_plans=plans,
        reference_changes=changes,
        configmaps_to_create=manifests_to_create,
        new_deployment=new_deployment,
        rollback_deployment=rollback_deployment,
        skipped_optional_references=skipped_optional,
        rendered_documents=len(rendered_docs),
    )


def dump_release_yaml(result: RehearsalResult) -> str:
    """Ordered multi-document YAML: ConfigMaps first, then new, then rollback."""
    docs: list[Any] = list(result.configmaps_to_create)
    docs.append(result.new_deployment)
    docs.append(result.rollback_deployment)
    return "---\n" + "\n---\n".join(
        yaml.safe_dump(doc, sort_keys=False, width=1000, allow_unicode=True).strip()
        for doc in docs
    ) + "\n"
