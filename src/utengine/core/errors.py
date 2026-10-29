"""Exception hierarchy. Every error that can stop trading is explicit and fail-closed."""


class EngineError(RuntimeError):
    """Base class."""


class ConfigError(EngineError):
    """Invalid, tampered or unsafe configuration."""


class KeyNotReadOnly(ConfigError):
    """The broker key's granted scopes include a write scope, or cannot be proven read-only. The engine never reads
    an account with such a key: the owner must provision a separate READ-ONLY key."""


class LiveExecutionNotAvailable(ConfigError):
    """Raised whenever anything asks for live order submission. Live is not implemented in this release and can
    never be enabled by configuration."""


class ShadowWriteViolation(EngineError):
    """A write was attempted while the engine has no write capability (shadow mode, or the read-only adapter)."""


class ReadOnlyViolation(ShadowWriteViolation):
    """The read-only eToro transport refused a request that is not on its read allowlist."""


class DataError(EngineError):
    """Missing, stale or inconsistent market data. The cycle holds; nothing is guessed."""


class StrategyError(EngineError):
    """A strategy module failed or produced an invalid decision."""


class BrokerError(EngineError):
    """A definite broker error (the broker answered)."""


class BrokerAmbiguousError(BrokerError):
    """The broker may or may not have processed a write (timeout, connection reset, unreadable success).
    The outcome is UNKNOWN and must be resolved by querying, never by resubmitting."""


class BrokerUnavailable(BrokerError):
    """A read could not be completed. Reconciliation cannot proceed; new orders are blocked."""


class IllegalTransition(EngineError):
    """An order-state transition that the state machine does not allow."""


class HaltActive(EngineError):
    """An action was refused because a halt is active."""


class SwitchError(EngineError):
    """A strategy switch was refused or failed."""
