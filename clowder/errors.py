"""Errors, each carrying the process exit code it should produce."""

from __future__ import annotations


class ClowderError(Exception):
    """Base for every error this tool raises on purpose."""

    exit_code = 1


class ConfigError(ClowderError):
    """The config file is missing, unreadable or wrong."""

    exit_code = 1


class UsageError(ClowderError):
    """The user asked for something the tool cannot mean."""

    exit_code = 1


class StateError(ClowderError):
    """The state file is corrupt or an id does not exist."""

    exit_code = 1


class MuxError(ClowderError):
    """The multiplexer could not be run, or its output made no sense."""

    exit_code = 2


class GitError(ClowderError):
    """A git command failed, or a tree was in no state to be worked in."""

    exit_code = 2


class DispatchError(ClowderError):
    """A task could not be sent to an agent."""

    exit_code = 2
