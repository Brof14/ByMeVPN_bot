"""Typed failures for provisioning operations with an ambiguous external outcome."""


class ProvisioningOutcomeUnknown(RuntimeError):
    """The external panel may have applied a change, but confirmation was lost.

    Callers must quarantine the associated payment for reconciliation instead of
    retrying an additive operation that could extend access twice.
    """
