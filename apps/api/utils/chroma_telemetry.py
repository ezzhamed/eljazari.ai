"""Local Chroma telemetry shims.

The pinned chromadb build in this project still emits noisy PostHog telemetry
errors even when anonymized telemetry is disabled through normal settings.
Provide an explicit no-op telemetry client so local runs stay quiet.
"""

from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override


class NoOpProductTelemetry(ProductTelemetryClient):
    """Disable Chroma product telemetry for local development and tests."""

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None
