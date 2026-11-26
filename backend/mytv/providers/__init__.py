from .base import Item, Provider, Source, SourceUnavailable
from .internet_archive import InternetArchive
from .local_library import LocalLibrary
from .direct_url import DirectUrl

# Add a new authorized source by writing a Provider subclass and listing it here.
PROVIDERS: dict[str, Provider] = {p.name: p for p in (InternetArchive(), LocalLibrary(), DirectUrl())}

__all__ = ["PROVIDERS", "Item", "Provider", "Source", "SourceUnavailable"]
