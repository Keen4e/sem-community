# Proposal — fix: Huawei forced charge never starts or stops on a zero-config install

> Local proposal only. No fork was created, no PR was opened, no live Home
> Assistant was touched, and no device was ever commanded. All verification is
> against the mock-based unit suite.

## Summary

On a **zero-config Huawei install** (no `inverter_device_id` written by the
config flow — the common case) the forced grid→battery charge can **never**
start or stop. Two independent defects, both only on the *charge* path:

1. **The autodetected battery device id never reached `HuaweiChargeAdapter`.**
   `HuaweiBatteryAdapter.__init__` autodetects the battery device into its own
   `_inverter_device_id` (used by the *discharge* path), but constructed
   `HuaweiChargeAdapter(hass, config)` from the **raw** `config`, which has no
   `inverter_device_id`. So `start_forced_charge` answered
   `FAILED "No inverter_device_id configured"` and `stop_forced_charge`
   answered nothing-to-stop — every cycle, forever. The discharge path worked
   the whole time, which is why it looked brand-specific rather than broken.

2. **`forcible_charge_soc` was called with an unsupported `duration` field.**
   The service's voluptuous schema is `{device_id, power, target_soc}` with
   `PREVENT_EXTRA`; `duration` belongs to the *time-based* `forcible_charge`
   service. The extra key makes the call raise
   (`extra keys not allowed @ data['duration']`), so the charge never went out.

Either defect alone is fatal; together they guarantee a silent, permanent
failure of Huawei grid charging.

## Root cause / evidence

* `coordinator/battery_adapters/huawei.py`
  * `HuaweiBatteryAdapter.__init__` — `self._inverter_device_id` is resolved
    (explicit config, else `_autodetect_battery_device()`), but the charge
    adapter was built **before** that, from the unmodified `config`.
  * `_stop_forced_charge()` reads
    `self._charge_adapter.config["inverter_device_id"]` — empty on every
    zero-config install.
* `coordinator/battery_adapters/force_charge.py`
  * `HuaweiChargeAdapter.start_forced_charge` passed
    `"duration": command.duration_minutes`.
* `huawei_solar` (`custom_components/huawei_solar/services.yaml` +
  `services.py`): `forcible_charge_soc` is registered with `SOC_SCHEMA`
  (`device_id`, `power`, `target_soc`) — **no `duration`**. `forcible_charge`
  (no `_soc`) is the one registered with `DURATION_SCHEMA`.

This was a known, documented gap: `docs/BUG_CLASSES.md` (#38 / #108) records
the force-charge flood sibling and the note that
`HuaweiBatteryAdapter` "autodetects it into its OWN `_inverter_device_id` and
does not pass it down", and the `stop_forced_charge` docstring said closing it
"needs a decision, not a sweep". This proposal is that decision, scoped to the
narrow, verifiable defect.

## The fix (2 production edits + tests)

* **`coordinator/battery_adapters/huawei.py`**
  * Build `HuaweiChargeAdapter` **after** the device id is resolved and hand it
    the autodetected id via a **copied** config:
    `HuaweiChargeAdapter(hass, {**config, "inverter_device_id": self._inverter_device_id})`.
    The copy means the caller's shared config is never mutated.
  * Refresh the module + `stop_forced_charge` review notes (the old "never
    passed down" caveat no longer holds).
* **`coordinator/battery_adapters/force_charge.py`**
  * Drop `"duration"` from the `forcible_charge_soc` call; the SOC target ends
    the charge. `ChargeCommand.duration_minutes` remains the scheduler's safety
    timeout; SEM stops the charge through the normal decision path.
  * The empty-`inverter_device_id` branch of `stop_forced_charge` stays and
    still returns **IDLE and silent** (#1005): a battery with no service
    target can never be charging, so there is nothing to stop — a permanent,
    knowable gap is DONE, not a transient FAILED retried forever.

### Failure handling (deliberately fail-closed)

* `start_forced_charge` with no device id → `FAILED` (unchanged): the caller
  retries next cycle rather than falsely reporting CHARGING.
* `stop_forced_charge` with no device id → `IDLE`, nothing sent (#1005, kept).
* A *real* service refusal on start/stop still surfaces as `FAILED` so
  honest-retry re-issues — never swallowed.

## Tests (TDD: RED → GREEN)

New file `tests/test_huawei_forced_charge_zero_config.py`:

* `TestForcibleChargeSocSchema`
  * `test_start_sends_no_duration_field` — the exact service data, no `duration`.
  * `test_a_schema_refusal_is_reported_not_hidden` — a refused write is `FAILED`.
* `TestZeroConfigDeviceIdPropagation`
  * `test_autodetected_id_reaches_the_charge_adapter`
  * `test_autodetect_does_not_mutate_the_shared_config`
  * `test_zero_config_force_charge_actually_starts` — end-to-end FORCE_CHARGE
    sends `forcible_charge_soc` with the autodetected device id.
  * `test_zero_config_stop_force_charge_actually_stops` — the stop path.
* `TestNoDeviceIdFailsClosed`
  * start `FAILED`; stop `IDLE` and silent.

Updated `tests/test_battery_charge_scheduler.py`: the old assertion encoded the
bug (expected `"duration": 240` in the call) and now expects the schema-valid
payload.

## How to run

```bash
rsync -a --delete --exclude=.git --exclude=node_modules \
  ./ /tmp/ha-config/custom_components/solar_energy_management/
cd /tmp/ha-config && PYTHONPATH=/tmp/ha-config \
  python3.14 -m pytest custom_components/solar_energy_management/tests/ -q
```

## Checklist (repo PR template)

* [x] Targets `develop`.
* [x] Tests added (regression + fail-closed); existing bug-encoding test corrected.
* [x] `ruff check` clean on touched files.
* [x] No new dependencies.
* [x] No user-facing text change (no translations needed).
* [x] No live device commanded; verification is unit-level only.
* [ ] Human maintainer review — merge is a maintainer decision.
