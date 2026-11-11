"""Provider-neutral errors. Provider adapters translate library-specific failures into these."""


class ProviderError(Exception):
    """Base class. `kind` is a stable machine-readable label stored with ingestion failures."""

    kind = "PROVIDER_ERROR"

    def __init__(self, message: str, *, provider: str | None = None, symbol: str | None = None):
        super().__init__(message)
        self.provider = provider
        self.symbol = symbol


class ProviderUnavailable(ProviderError):
    """Network failure, timeout, or an unexpected provider/library exception."""

    kind = "UNAVAILABLE"


class RateLimited(ProviderError):
    kind = "RATE_LIMITED"


class NoDataReturned(ProviderError):
    """The provider answered but returned no usable rows (unknown symbol, out-of-range request, closed market...)."""

    kind = "NO_DATA"


class MalformedResponse(ProviderError):
    """The response did not have the expected structure (missing columns, wrong types)."""

    kind = "MALFORMED"


class InvalidRequest(ProviderError):
    """The request itself is unsupported (e.g. an interval or range the provider cannot serve)."""

    kind = "INVALID_REQUEST"


class UnsupportedCapability(ProviderError):
    """The adapter does not (or is not known to) support the requested capability."""

    kind = "UNSUPPORTED_CAPABILITY"


class InstrumentDisabled(Exception):
    """Raised before any provider call when an instrument is disabled (e.g. futures)."""
