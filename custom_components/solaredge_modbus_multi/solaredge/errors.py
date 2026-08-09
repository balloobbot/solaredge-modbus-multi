"""Errors raised by the SolarEdge device library."""

from __future__ import annotations


class SolarEdgeError(Exception):
    """Base class for every error this library raises itself."""


class DeviceInvalid(SolarEdgeError):
    """The device at this address is not the SolarEdge device we expected."""


class DeviceNotSetUp(SolarEdgeError):
    """A device was polled or written before ``async_setup`` ran."""
