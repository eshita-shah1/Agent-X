"""Maps provider codes to adapter instances. Selection is always explicit: there is no fallback chain,
because silently switching providers would mix datasets of different provenance."""

from app.marketdata.errors import UnsupportedCapability
from app.marketdata.providers.yahoo import YahooFinanceProvider


class ProviderRegistry:
    def __init__(self, providers: dict[str, object]):
        self._providers = dict(providers)

    def get(self, code: str):
        try:
            return self._providers[code]
        except KeyError:
            raise UnsupportedCapability(f"provider {code!r} is not configured", provider=code) from None

    def codes(self) -> list[str]:
        return sorted(self._providers)

    def descriptors(self):
        return [p.descriptor for p in self._providers.values()]


def build_default_registry() -> ProviderRegistry:
    return ProviderRegistry({"yahoo": YahooFinanceProvider()})
