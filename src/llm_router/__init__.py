"""Source-agnostic, capability-aware LLM routing."""

from .config import load_config
from .discovery import DiscoverySettings, ModelDiscovery
from .errors import AllModelsFailed, ConfigError, NoEligibleModel, RequestError, RouterError
from .provisioning import OllamaProvisioner, ProvisioningSettings
from .router import LLMRouter
from .schema import QueryRequest, RouterConfig

__all__ = [
    "AllModelsFailed",
    "ConfigError",
    "DiscoverySettings",
    "LLMRouter",
    "ModelDiscovery",
    "NoEligibleModel",
    "OllamaProvisioner",
    "ProvisioningSettings",
    "QueryRequest",
    "RequestError",
    "RouterConfig",
    "RouterError",
    "load_config",
]

__version__ = "0.3.10"
