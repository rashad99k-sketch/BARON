"""External market-intelligence providers for ATOM-BOOT.

The layer is deliberately advisory: it enriches candidates and Telegram alerts,
but it never calls order execution and never becomes an entry authority.
"""
from .models import IntelligenceSnapshot, IntelligenceSignal
from .fusion import IntelligenceFusion
from .service import ExternalIntelligenceService

__all__ = ["IntelligenceSnapshot", "IntelligenceSignal", "IntelligenceFusion", "ExternalIntelligenceService"]
