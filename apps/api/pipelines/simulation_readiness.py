"""Fail closed unless a generated aircraft has a verified SITL flight bundle."""

from typing import Any, Mapping

_GENERATED_KEYS = (
    "urdf_path",
    "sdf_path",
    "model_manifest_path",
    "resolved_drone_sha256",
    "resolved_drone_spec",
    "selected_components",
    "flight_model_manifest_path",
)


def requests_generated_model(config: Mapping[str, Any]) -> bool:
    return any(config.get(key) for key in _GENERATED_KEYS) or config.get("bom_status") == "selected"


def generated_model_block_reason(config: Mapping[str, Any]) -> str | None:
    """Do not let a requested artifact silently become the default x500.

    Structural artifacts and manifests that merely claim flight readiness are
    not flyable. Only a flight bundle whose files re-hash to its manifest and
    whose manifest names the current resolved specification may be loaded;
    the SITL backend then attests the spawned model separately. Reference-model
    runs without generated artifacts remain available.
    """
    if not requests_generated_model(config):
        return None
    manifest = config.get("flight_model_manifest_path")
    if not manifest:
        detail = config.get("flight_model_block_reason")
        return (
            "Generated-aircraft flight validation is unavailable: no verified flight "
            "bundle exists for this design. Structural artifacts are not flight-ready; "
            "running the reference x500 would not validate this design."
            + (f" Flight model generation reported: {detail}" if detail else "")
        )
    from pipelines.flight_model import verify_flight_bundle

    try:
        verify_flight_bundle(manifest, str(config.get("resolved_drone_sha256") or ""))
    except Exception as exc:  # noqa: BLE001 - any failure must block the flight
        return f"Generated flight bundle failed verification: {exc}"
    return None
