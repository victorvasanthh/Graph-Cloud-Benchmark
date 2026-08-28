"""Exception types.

The runner distinguishes three failure modes because they mean different
things in a published result table:

  * ConfigurationError - our fault, the run never started. Fix and re-run.
  * ConnectionFailure  - the target was unreachable. Reported as "unavailable",
                         never as a slow result, because a connection error
                         timed as latency would silently defame a database.
  * WorkloadFailure    - the target connected but could not execute the query
                         (unsupported syntax, timeout, OOM). Reported as an
                         explicit failure with the driver's message attached.
"""


class BenchmarkError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(BenchmarkError):
    """Missing credentials, malformed YAML, or an unknown database kind."""


class ConnectionFailure(BenchmarkError):
    """The database could not be reached or authenticated against."""


class WorkloadFailure(BenchmarkError):
    """A query was rejected, timed out, or returned an unusable shape."""


class QueryTimeout(WorkloadFailure):
    """The query exceeded its wall-clock bound and was abandoned.

    Distinct from a WorkloadFailure that the engine rejected. A timeout says
    the engine accepted the query, was still working when the bound expired,
    and we stopped waiting - so the honest report is "did not complete within
    N seconds", never "failed" and never a latency measurement.

    Reported separately for a practical reason too: a rejected query is a bug
    in the harness, while a timeout is usually a property of the engine at the
    configured resource cap, and conflating them sends people to fix the wrong
    thing.
    """


class ConnectionLost(WorkloadFailure):
    """The connection died while the query was running.

    Distinct from a rejected query and distinct from a timeout. The engine
    accepted the statement and then the transport went away - a server that
    restarted, ran out of memory, or a proxy that closed an idle socket.

    The distinction matters more than it looks. When one heavy workload kills
    the connection, every workload after it fails too, and reading those as
    "unsupported" would condemn queries that were never really attempted. A
    ConnectionLost is a signal to wait for the engine to come back and retry,
    not a verdict on the statement.
    """


class UnsupportedWorkload(WorkloadFailure):
    """This engine has no equivalent for the workload.

    Distinct from a failure: an engine that genuinely lacks a feature should
    show as "n/a" in the report rather than as a loss. Conflating the two is
    the single most common way a benchmark misleads.
    """
