"""Local execution safety primitives."""

from horizonx.security.environment_policy import (
    build_child_environment,
    redact_secrets,
    trust_boundary_metadata,
)
from horizonx.security.process import (
    collect_process_output,
    drain_stream,
    spawn_process,
    spawn_shell,
    terminate_process_tree,
)

__all__ = [
    "build_child_environment",
    "collect_process_output",
    "drain_stream",
    "redact_secrets",
    "spawn_process",
    "spawn_shell",
    "terminate_process_tree",
    "trust_boundary_metadata",
]
