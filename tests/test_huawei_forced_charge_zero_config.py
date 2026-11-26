"""Huawei forced charge (grid→battery) fails two ways on a real install.

A zero-config Huawei install (the common one — no ``inverter_device_id``
written by the flow) can NEVER start or stop a forced charge. Two independent
defects, both only on the *charge* path:

1. **The autodetected device id is dropped.** ``HuaweiBatteryAdapter``
   autodetects the battery device into its OWN ``_inverter_device_id`` (used by
   the discharge path), but constructs ``HuaweiChargeAdapter`` with the RAW
   ``config``. The charge adapter reads ``config["inverter_device_id"]``, which
   is empty, so ``start_forced_charge`` answers
   ``FAILED "No inverter_device_id configured"`` and ``stop_forced_charge``
   answers nothing-to-stop — every cycle, forever.

2. **The service carries an unsupported field.** ``huawei_solar``'s
   ``forcible_charge_soc`` is registered against ``{device_id, power,
   target_soc}`` (voluptuous ``PREVENT_EXTRA``); ``start_forced_charge`` also
   passes ``duration``, which the schema rejects — the call raises and the
   charge never goes out. ``duration`` belongs to the *time-based*
   ``forcible_charge`` service, not ``forcible_charge_soc`` (the SOC target is
   what ends this one).

Both are asserted here against the real adapter code paths, never hardware.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator.battery_adapters.force_charge import (
    ChargeCommand,
    ChargeCommandStatus,
    HuaweiChargeAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
    HuaweiBatteryAdapter,
)

_START = ("huawei_solar", "forcible_charge_soc")
_STOP = ("huawei_solar", "stop_forcible_charge")

#: The ``huawei_solar`` battery device the services target — identifier with
#: ``connected_energy_storage`` (the combined-storage device).
_BATTERY_DEVICE_ID = "batterydev123"


def _hass():
    """A hass whose huawei_solar is loaded and whose forcible sensor reads
    nothing (so the startup orphan-clear is a no-op and the cycle proceeds)."""
    h = MagicMock()
    h.services.async_call = AsyncMock()
    h.data = {"huawei_solar": {}}
    h.config_entries.async_entries = MagicMock(return_value=[])
    h.states.async_all = MagicMock(return_value=[])
    return h


def _registry_with_huawei_battery(hass):
    """Patch the device registry so autodetection finds a Huawei battery.

    Mirrors ``test_battery_arbitrage_523.test_huawei_autodetects_battery_device_zero_config``:
    asked per config entry (#1026), not read off ``reg.devices``.
    """
    dev = MagicMock()
    dev.id = _BATTERY_DEVICE_ID
    dev.identifiers = {("huawei_solar", "BT2470369058/connected_energy_storage")}
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(entry_id="huawei-entry")
    ]
    reg = MagicMock(spec=["async_get"])
    return [
        patch("homeassistant.helpers.device_registry.async_get", return_value=reg),
        patch(
            "homeassistant.helpers.device_registry.async_entries_for_config_entry",
            return_value=[dev],
        ),
    ]


# ── Defect 2: the service does not accept ``duration`` ────────────────────


class TestForcibleChargeSocSchema:
    """``forcible_charge_soc`` takes device_id + target_soc + power ONLY."""

    @pytest.mark.asyncio
    async def test_start_sends_no_duration_field(self):
        hass = _hass()
        adapter = HuaweiChargeAdapter(hass, {"inverter_device_id": "dev1"})

        status = await adapter.start_forced_charge(
            ChargeCommand(target_soc=80.0, max_power_w=3000, duration_minutes=240))

        assert status.status is ChargeCommandStatus.CHARGING
        hass.services.async_call.assert_awaited_once_with(
            "huawei_solar",
            "forcible_charge_soc",
            {"device_id": "dev1", "target_soc": 80, "power": 3000},
        )

    @pytest.mark.asyncio
    async def test_a_schema_refusal_is_reported_not_hidden(self):
        """A refused write must surface as FAILED so honest-retry re-issues —
        never a silent CHARGING that no hardware ever received."""
        hass = _hass()
        hass.services.async_call = AsyncMock(
            side_effect=Exception("extra keys not allowed @ data['duration']"))
        adapter = HuaweiChargeAdapter(hass, {"inverter_device_id": "dev1"})

        status = await adapter.start_forced_charge(
            ChargeCommand(target_soc=80.0, max_power_w=3000))

        assert status.status is ChargeCommandStatus.FAILED
        assert not adapter.is_active


# ── Defect 1: the autodetected device id never reaches the charge adapter ──


class TestZeroConfigDeviceIdPropagation:

    def test_autodetected_id_reaches_the_charge_adapter(self):
        hass = _hass()
        patches = _registry_with_huawei_battery(hass)
        with patches[0], patches[1]:
            adapter = HuaweiBatteryAdapter(hass, {})

        # sanity: the discharge path already sees it…
        assert adapter._inverter_device_id == _BATTERY_DEVICE_ID
        # …and now so does the charge path (the defect: this was "").
        assert adapter._charge_adapter.config.get("inverter_device_id") == \
            _BATTERY_DEVICE_ID

    def test_autodetect_does_not_mutate_the_shared_config(self):
        """The id is handed to the child adapter, not written back into the
        caller's dict (the coordinator shares it across the battery)."""
        hass = _hass()
        cfg: dict = {}
        patches = _registry_with_huawei_battery(hass)
        with patches[0], patches[1]:
            HuaweiBatteryAdapter(hass, cfg)

        assert "inverter_device_id" not in cfg

    @pytest.mark.asyncio
    async def test_zero_config_force_charge_actually_starts(self):
        """The reported bug end-to-end: with NO configured device id, a
        FORCE_CHARGE must send forcible_charge_soc (autodetected device,
        no duration)."""
        hass = _hass()
        patches = _registry_with_huawei_battery(hass)
        with patches[0], patches[1]:
            adapter = HuaweiBatteryAdapter(hass, {})
        hass.services.async_call.reset_mock()

        await adapter.command_force_charge(
            target_soc=100.0, charge_power_w=5000, duration_min=60)

        call = hass.services.async_call.await_args
        assert (call.args[0], call.args[1]) == _START
        assert call.args[2] == {
            "device_id": _BATTERY_DEVICE_ID, "target_soc": 100, "power": 5000}
        assert adapter.last_intent.value == "force_charge"

    @pytest.mark.asyncio
    async def test_zero_config_stop_force_charge_actually_stops(self):
        """The stop path: a charge SEM started must be stoppable. Before the
        fix the charge adapter saw no device id and sent nothing."""
        hass = _hass()
        patches = _registry_with_huawei_battery(hass)
        with patches[0], patches[1]:
            adapter = HuaweiBatteryAdapter(hass, {})
        # A charge is in flight (SEM started it this lifetime).
        adapter._forcible_charging = True
        hass.services.async_call.reset_mock()

        await adapter.command_stop_force_charge()

        calls = [(c.args[0], c.args[1]) for c in hass.services.async_call.await_args_list]
        assert _STOP in calls
        stop = next(c for c in hass.services.async_call.await_args_list
                    if (c.args[0], c.args[1]) == _STOP)
        assert stop.args[2] == {"device_id": _BATTERY_DEVICE_ID}


# ── Fail-closed: no device id is still a real install, not a crash ─────────


class TestNoDeviceIdFailsClosed:
    """A Huawei install with NO battery device (number-entity only, or an
    unreadable registry) must keep the #1005 behaviour: start FAILED, stop
    IDLE and silent — a request that can never run is DONE, not retried."""

    @pytest.mark.asyncio
    async def test_start_fails_without_a_device_id(self):
        hass = _hass()
        adapter = HuaweiChargeAdapter(hass, {})

        status = await adapter.start_forced_charge(
            ChargeCommand(target_soc=80.0, max_power_w=3000))

        assert status.status is ChargeCommandStatus.FAILED
        assert "No inverter_device_id" in status.message

    @pytest.mark.asyncio
    async def test_stop_is_idle_and_silent_without_a_device_id(self):
        hass = _hass()
        adapter = HuaweiChargeAdapter(hass, {})

        status = await adapter.stop_forced_charge()

        assert status.status is ChargeCommandStatus.IDLE
        assert hass.services.async_call.await_count == 0
