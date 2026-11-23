"""FastAPI service: clean shared Helm values overrides across chart versions.

Endpoints:
* GET  /health                  liveness probe
* POST /api/v1/solve            shared-values cleanup analysis
* POST /api/v1/rehearse-rollout ConfigMap-safe rollout rehearsal (no cluster)

Swagger UI: /docs  (built-in examples are prefilled there).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .rehearsal import (
    RehearsalError,
    RehearsalRequestError,
    dump_release_yaml,
    parse_manifest_field,
    plan_rollout,
)
from .solver import BaselineFailure, MAX_CANDIDATES, solve
from .values_tools import load_values

app = FastAPI(
    title="Shared Helm Values Cleanup & Rollout Rehearsal API",
    version="1.1.0",
    description=(
        "Renders each chart version with the shared values override, then finds "
        "the largest set (up to 8) of non-contained mapping-key paths that can be "
        "deleted while keeping every version's rendered manifests identical. "
        "Comments, document order and mapping-key order are ignored; list order is kept. "
        "The /api/v1/rehearse-rollout endpoint additionally plans a ConfigMap-safe "
        "rolling upgrade: changed/new ConfigMaps become immutable content-hashed copies, "
        "live objects are never modified and no cluster connection is made."
    ),
)

# Default chart root for the bundled two-chart dataset ("mounted directory").
DEFAULT_CHART_ROOT = Path(
    "/workspace/mounted-charts"
).resolve() if Path("/workspace/mounted-charts").is_dir() else Path("/charts")


class ChartVersion(BaseModel):
    label: str = Field(..., description="Stable label, e.g. 'production-v1'")
    chart_dir: str = Field(..., description="Absolute path to this version's chart directory")

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("label must not be blank")
        return v


class SolveRequest(BaseModel):
    values_override_path: str = Field(
        ..., description="Absolute path to the shared values override YAML"
    )
    versions: list[ChartVersion] = Field(
        ..., min_length=1, description="Chart versions that share this override"
    )
    release_name: str = Field("platform-rel", description="Fixed helm release name")
    namespace: str = Field("platform", description="Fixed helm namespace")

    @field_validator("values_override_path", "release_name", "namespace")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("versions")
    @classmethod
    def unique_labels(cls, v: list[ChartVersion]) -> list[ChartVersion]:
        labels = [item.label for item in v]
        if len(set(labels)) != len(labels):
            raise ValueError("version labels must be unique")
        return v


class YAMLResponse(Response):
    """Minimal YAML response for clients sending Accept: application/yaml."""

    media_type = "application/yaml"

    def render(self, content: Any) -> bytes:
        return yaml.safe_dump(
            content,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        ).encode("utf-8")


class VersionReport(BaseModel):
    label: str
    chart_dir: str
    render_ok: bool
    manifests_match_baseline: bool


class SolveResponse(BaseModel):
    cleaned_yaml: str = Field(..., description="Override YAML with the removable keys deleted")
    removed_paths: list[str] = Field(..., description="Mapping-key paths safe to delete")
    candidate_paths: list[str] = Field(
        ..., description=f"At most {MAX_CANDIDATES} non-contained candidate paths considered"
    )
    kept_paths: list[str] = Field(..., description="All other leaf paths, retained as-is")
    total_leaf_paths: int
    candidate_cap_applied: bool
    verified: bool = Field(..., description="Final cleaned YAML re-rendered and matched on every version")
    versions: list[VersionReport]
    combinations_evaluated: int
    render_failures: int
    release_name: str
    namespace: str


# --------------------------------------------------------------------------- #
# Rollout rehearsal (/api/v1/rehearse-rollout)
# --------------------------------------------------------------------------- #

class RehearseRequest(BaseModel):
    current_deployment: dict[str, Any] | list[dict[str, Any]] | str = Field(
        ...,
        description=(
            "The live Deployment manifest: a single object, a one-element list or a "
            "YAML document string."
        ),
    )
    current_configmaps: list[dict[str, Any]] | str = Field(
        ...,
        description=(
            "All ConfigMaps of the same namespace: a list of objects or a "
            "multi-document YAML string."
        ),
    )
    chart_dir: str = Field(
        ..., description="Absolute path to the NEW chart version inside the mounted directory"
    )
    values_path: str | None = Field(
        None,
        description="Absolute path to the values file for the new version; null renders chart defaults",
    )
    release_name: str = Field("platform-rel", description="Fixed helm release name")
    namespace: str = Field("platform", description="Namespace of the live workload")

    @field_validator("chart_dir", "release_name", "namespace")
    @classmethod
    def not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v


class ConfigMapPlanModel(BaseModel):
    source_name: str
    copy_name: str
    immutable: bool
    reused: bool
    reason: str


class ReferenceChangeModel(BaseModel):
    kind: str
    location: str
    source_name: str
    target_name: str
    rewritten: bool


class RehearseResponse(BaseModel):
    release_name: str
    namespace: str
    deployment_name: str
    rendered_documents: int
    configmap_plans: list[ConfigMapPlanModel]
    reference_changes: list[ReferenceChangeModel]
    skipped_optional_references: list[str]
    configmaps_to_create: list[dict[str, Any]] = Field(
        ..., description="Immutable ConfigMap copies to create (ordered before the Deployments)"
    )
    new_deployment: dict[str, Any] = Field(
        ..., description="New-version Deployment with references rewritten onto the copies"
    )
    rollback_deployment: dict[str, Any] = Field(
        ..., description="Apply-able snapshot of the original Deployment for rollback"
    )
    release_yaml: str = Field(
        ...,
        description="Ordered multi-document YAML: ConfigMaps first, then the new Deployment, then rollback",
    )


def _example_request(chart: str, v1: str, v2: str) -> dict[str, Any]:
    return {
        "values_override_path": f"/workspace/overrides/{chart}.values.yaml",
        "release_name": "platform-rel",
        "namespace": "platform",
        "versions": [
            {
                "label": "production-v1",
                "chart_dir": f"/workspace/mounted-charts/{chart}/{v1}",
            },
            {
                "label": "staging-v2",
                "chart_dir": f"/workspace/mounted-charts/{chart}/{v2}",
            },
        ],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/solve",
    response_model=SolveResponse,
    responses={
        200: {
            "description": "Cleanup result (JSON by default, YAML with Accept: application/yaml)",
            "content": {
                "application/json": {},
                "application/yaml": {},
            },
        },
        400: {"description": "Invalid request / unreadable values file"},
        422: {"description": "Baseline helm template failed for a version"},
    },
)
def solve_endpoint(
    req: SolveRequest, accept: str = Header(default="")
) -> SolveResponse | YAMLResponse:
    values_path = Path(req.values_override_path)
    if not values_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"values override not found or not a file: {values_path}",
        )
    try:
        values = load_values(values_path)
    except (yaml.YAMLError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"failed to parse values YAML: {exc}"
        ) from exc

    versions = [item.model_dump() for item in req.versions]
    try:
        result = solve(
            values=values,
            versions=versions,
            release_name=req.release_name,
            namespace=req.namespace,
        )
    except BaselineFailure as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = SolveResponse(
        cleaned_yaml=result.cleaned_yaml,
        removed_paths=result.removed_paths,
        candidate_paths=result.candidate_paths,
        kept_paths=result.kept_paths,
        total_leaf_paths=result.total_leaf_paths,
        candidate_cap_applied=result.capped,
        verified=result.verified,
        versions=[VersionReport(**report) for report in result.versions],
        combinations_evaluated=result.combinations_evaluated,
        render_failures=result.render_failures,
        release_name=req.release_name,
        namespace=req.namespace,
    )
    # YAML response for clients sending Accept: application/yaml.
    if "yaml" in accept.lower():
        return YAMLResponse(payload.model_dump())
    return payload


# --------------------------------------------------------------------------- #
# Rollout rehearsal endpoint
# --------------------------------------------------------------------------- #

def _read_manifest_file(path: Path, field_name: str) -> str:
    if not path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} not found or not a file: {path}",
        )
    return path.read_text(encoding="utf-8")


def _rehearse_example(conflict: bool = False) -> dict[str, Any]:
    cm_file = "configmaps.conflict.yaml" if conflict else "configmaps.yaml"
    base = Path("/workspace/live-manifests/payment-web")
    fallback = Path(__file__).resolve().parent.parent / "live-manifests" / "payment-web"
    root = base if base.is_dir() else fallback
    return {
        "release_name": "platform-rel",
        "namespace": "platform",
        "chart_dir": "/workspace/mounted-charts/payment-web/v3",
        "values_path": "/workspace/rehearsal-values/payment-web.rehearsal.values.yaml",
        "current_deployment": _read_manifest_file(root / "deployment.yaml", "current_deployment"),
        "current_configmaps": _read_manifest_file(root / cm_file, "current_configmaps"),
    }


@app.post(
    "/api/v1/rehearse-rollout",
    response_model=RehearseResponse,
    responses={
        200: {
            "description": "Rollout plan (JSON by default, YAML with Accept: application/yaml)",
            "content": {
                "application/json": {},
                "application/yaml": {},
            },
        },
        400: {"description": "Invalid request / unreadable chart or values path"},
        422: {"description": "Planning rejected: missing config, missing volume key, selector change, name conflict, render failure"},
    },
)
def rehearse_rollout_endpoint(
    req: RehearseRequest, accept: str = Header(default="")
) -> RehearseResponse | YAMLResponse:
    try:
        deployment_docs = parse_manifest_field(req.current_deployment, "current_deployment")
        configmap_docs = parse_manifest_field(req.current_configmaps, "current_configmaps")
    except RehearsalRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.reasons) from exc

    if len(deployment_docs) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"current_deployment must contain exactly one document, got {len(deployment_docs)}",
        )

    chart_dir = Path(req.chart_dir)
    values_path = Path(req.values_path) if req.values_path else None

    try:
        result = plan_rollout(
            current_deployment=deployment_docs[0],
            live_configmaps=configmap_docs,
            chart_dir=chart_dir,
            values_file=values_path,
            release_name=req.release_name,
            namespace=req.namespace,
        )
    except RehearsalRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.reasons) from exc
    except RehearsalError as exc:
        # No release manifests are returned: only the reasons.
        raise HTTPException(status_code=422, detail=exc.reasons) from exc

    payload = RehearseResponse(
        release_name=result.release_name,
        namespace=result.namespace,
        deployment_name=result.deployment_name,
        rendered_documents=result.rendered_documents,
        configmap_plans=[ConfigMapPlanModel(**vars(p)) for p in result.configmap_plans],
        reference_changes=[ReferenceChangeModel(**vars(c)) for c in result.reference_changes],
        skipped_optional_references=result.skipped_optional_references,
        configmaps_to_create=result.configmaps_to_create,
        new_deployment=result.new_deployment,
        rollback_deployment=result.rollback_deployment,
        release_yaml=dump_release_yaml(result),
    )
    if "yaml" in accept.lower():
        return YAMLResponse(payload.model_dump())
    return payload


# Attach the built-in examples to the OpenAPI schema after app creation.
def _inject_examples() -> None:
    path_schema = app.openapi()
    solve_op = path_schema["paths"]["/api/v1/solve"]["post"]
    solve_op["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/SolveRequest"},
                "examples": {
                    "orders-api (v1 production + v2 staging)": {
                        "summary": "orders-api shared override cleanup",
                        "value": _example_request("orders-api", "v1", "v2"),
                    },
                    "payment-web (v1 production + v2 staging)": {
                        "summary": "payment-web shared override cleanup",
                        "value": _example_request("payment-web", "v1", "v2"),
                    },
                },
            }
        },
    }
    rehearse_op = path_schema["paths"]["/api/v1/rehearse-rollout"]["post"]
    rehearse_op["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/RehearseRequest"},
                "examples": {
                    "payment-web v2 -> v3 (happy path)": {
                        "summary": (
                            "Immutable copy for the changed chart ConfigMap, external reference kept, "
                            "optional references skipped"
                        ),
                        "value": _rehearse_example(conflict=False),
                    },
                    "payment-web v2 -> v3 (same-name conflict)": {
                        "summary": "Mutable squatter on the deterministic copy name -> 422 with reasons",
                        "value": _rehearse_example(conflict=True),
                    },
                },
            }
        },
    }
    app.openapi_schema = path_schema


_inject_examples()
