"""Device ID scanner for SolarEdge Modbus Multi.

A SolarEdge gateway hosts up to 32 inverters as separate Modbus unit IDs behind
one socket, and nothing announces which IDs are in use — so setup has to ask.
The scan has to tell three cases apart:

* a SolarEdge inverter,
* some other Modbus device that answered,
* nothing at that ID at all.

This used to be done by writing hand-assembled Modbus/TCP frames to a bare
asyncio socket and comparing the reply byte for byte, because the Modbus client
of the day collapsed the last two cases into one exception. It is now an
ordinary read of the SunSpec identity registers: a timeout is nobody home, any
answer at all is *something*, and the "SunS" marker plus a SolarEdge
manufacturer string is an inverter.

Original approach based on work by thargy: https://github.com/thargy/modbus-scanner
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.exceptions import HomeAssistantError
from modbus_connection import (
    ModbusConnection,
    ModbusConnectionError,
    ModbusError,
    ModbusTcpParams,
)

from .solaredge import SUNSPEC_BASE_ADDRESS

_LOGGER = logging.getLogger(__name__)

# The nine registers the identity of a SolarEdge inverter fits in: the "SunS"
# marker, the common model's ID and length, and enough of the manufacturer
# string to recognise it.
_SIGNATURE_COUNT = 9
_SUNSPEC_MARKER = (0x5375, 0x6E53)  # "SunS"
_COMMON_MODEL_ID = 1
_COMMON_MODEL_LENGTH = 65
_MANUFACTURER_PREFIX = "SolarEdge"


class SolarEdgeDeviceScanner:
    """Probe a range of Modbus unit IDs for SolarEdge inverters."""

    NOT_FOUND = 0
    FOUND = 1
    FOUND_INV = 2

    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout: float = 5.0,
        scan_retries: int = 3,
        scan_timeout: float = 3.0,
    ):
        """Initialize the SolarEdge device scanner.

        Args:
            host: Target host address.
            port: Target port number.
            connect_timeout: Seconds to wait for the socket to open.
            scan_retries: Number of attempts per unit ID before giving up.
            scan_timeout: Seconds to wait for one unit ID to answer.
        """
        self._connect_timeout = connect_timeout
        self._scan_retries = scan_retries
        self._host = host
        self._port = port
        self._connection = ModbusConnection(
            ModbusTcpParams(host=host, port=port), timeout=scan_timeout
        )

        self.inverters: list[int] = []

    async def scan_list(
        self,
        device_list: list[int],
        progress_callback: callable = None,
    ) -> list[int]:
        """Scan a list of device IDs for SolarEdge inverters.

        Args:
            device_list: List of Modbus device IDs to scan.
            progress_callback: Optional callback to report progress.
                               Called with (scanned_count, total_count).

        Returns:
            List of device IDs that are SolarEdge inverters.
        """
        total = len(device_list)
        scanned = 0

        if progress_callback:
            await progress_callback(scanned, total)

        for device_id in device_list:
            _LOGGER.debug(f"Calling scan_device_id on device_id={device_id}")
            if await self.scan_device_id(device_id) == self.FOUND_INV:
                self.inverters.append(device_id)

            scanned += 1
            if progress_callback:
                _LOGGER.debug(f"scan_list progress: {scanned} of {total}")
                await progress_callback(scanned, total)

        return self.inverters

    async def check_list(self, device_list: list[int]) -> dict[str, list[int]]:
        """Check a list of device IDs and categorize the results.

        Args:
            device_list: List of Modbus device IDs to validate.

        Returns:
            Dictionary with three lists:
            - "inverters": Device IDs that are SolarEdge inverters
            - "other_devices": Device IDs that responded but aren't SolarEdge inverters
            - "no_response": Device IDs that didn't respond or timed out
        """
        inverters: list[int] = []
        other_devices: list[int] = []
        no_response: list[int] = []

        for device_id in device_list:
            result = await self.scan_device_id(device_id)
            if result == self.FOUND_INV:
                inverters.append(device_id)
            elif result == self.FOUND:
                other_devices.append(device_id)
            else:
                no_response.append(device_id)

        return {
            "inverters": inverters,
            "other_devices": other_devices,
            "no_response": no_response,
        }

    async def connect(self) -> None:
        """Open the link, so an unreachable host fails before the scan starts."""
        attempt = 1

        while attempt <= self._scan_retries:
            try:
                _LOGGER.debug(f"Connecting to {self._host}:{self._port} ...")
                async with asyncio.timeout(self._connect_timeout):
                    await self._connection.connect()
            except (ModbusError, TimeoutError) as e:
                attempt += 1
                _LOGGER.warning(f"Unable to connect to {self._host}:{self._port}: {e}")
                if attempt <= self._scan_retries:
                    await asyncio.sleep(1.0)
            else:
                return

        raise HomeAssistantError(
            f"Unable to connect to {self._host}:{self._port} "
            f"after {self._scan_retries} attempts."
        )

    async def disconnect(self) -> None:
        """Close the connection the scan used."""
        await self._connection.close()

    def device_is_inverter(self, registers: list[int]) -> int:
        """Classify a device from its answer to the identity read.

        Returns:
            FOUND_INV (2) if a SolarEdge inverter was detected.
            FOUND (1) if some other Modbus device answered.
        """
        if len(registers) < _SIGNATURE_COUNT:
            return self.FOUND
        if tuple(registers[0:2]) != _SUNSPEC_MARKER:
            return self.FOUND
        if registers[2] != _COMMON_MODEL_ID or registers[3] != _COMMON_MODEL_LENGTH:
            return self.FOUND

        raw = b"".join((word & 0xFFFF).to_bytes(2, "big") for word in registers[4:9])
        manufacturer = raw.decode("ascii", errors="ignore")
        if not manufacturer.startswith(_MANUFACTURER_PREFIX):
            return self.FOUND

        return self.FOUND_INV

    async def scan_device_id(self, device_id: int) -> int:
        """Scan a specific Modbus device ID for a SolarEdge inverter.

        Args:
            device_id: The Modbus device ID to scan (1-247).

        Returns:
            FOUND_INV (2) if a SolarEdge inverter was detected.
            FOUND (1) if a non-inverter Modbus device responded.
            NOT_FOUND (0) if no valid response was received.

        Raises:
            HomeAssistantError: If the link to the gateway itself fails.
        """
        unit = self._connection.for_unit(device_id)

        for attempt in range(1, self._scan_retries + 1):
            try:
                _LOGGER.debug(f"Scanning ID: {device_id} ...")
                registers = await unit.read_holding_registers(
                    SUNSPEC_BASE_ADDRESS, _SIGNATURE_COUNT
                )

            except ModbusConnectionError as e:
                # The gateway itself went away; retrying other IDs is pointless.
                raise HomeAssistantError(
                    f"Connection to {self._host}:{self._port} lost while "
                    f"scanning ID {device_id}: {e}"
                )

            except TimeoutError:
                _LOGGER.debug(f" ID {device_id} did not answer (attempt {attempt})")
                continue

            except ModbusError as e:
                # Something is there — it just doesn't serve these registers.
                _LOGGER.debug(f" ID {device_id} answered with an error: {e}")
                return self.FOUND

            result = self.device_is_inverter(registers)
            if result == self.FOUND_INV:
                _LOGGER.debug(f" {device_id} is INVERTER")
            else:
                _LOGGER.warning(
                    f"Scanned device {device_id} did not match signature: {registers}"
                )
            return result

        _LOGGER.debug(f" No device found at ID {device_id}")
        return self.NOT_FOUND
