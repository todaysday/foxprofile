"""Extension-owned contracts and policies, independent of application startup."""

from .composition import ExtensionComposition, create_extension_composition

__all__ = ["ExtensionComposition", "create_extension_composition"]
