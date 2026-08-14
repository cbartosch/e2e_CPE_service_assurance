"""Fixture-backed ACS / TR-069 northbound, and the TR-181 `Device.WiFi.*` read the predictive scan
is built on.

The tree shape follows the specification's reference pipeline, whose synthetic GenieACS-shaped
responses it names as "a reasonable starting shape for that simulator, not a confirmed production
integration". So: the *parameter paths* below (`Device.WiFi.Radio.1.Channel`,
`Device.WiFi.AccessPoint.1.AssociatedDevice.2.SignalStrength`) are real TR-181 data-model paths and
are the one part of this file that is not invented -- TR-181 is a published Broadband Forum model.
Everything around them is ours: the envelope, the `data_quality_notes` vocabulary, the diagnostic
names, the action parameter names. Gaps CPE-1 to CPE-6.

**Masking happens here, at collection.** `read_wifi_status` masks every client MAC before it
returns, using `lpr_cpe.security.redaction.mask_mac` -- one owner for the rule, shared with the
model-call boundary and the audit log. Masking in the detector would be too late in a way that is
easy to miss: the unmasked payload would already have been returned, and anything that logged or
checkpointed the adapter's result would have persisted it. The specification puts this control "at
the point of collection", and this module is that point.

Three device conditions the detectors need, all of them fixture-driven rather than random:

* **healthy** -- informed minutes ago, full tree.
* **stale** -- `SVC-VQ-002-A-02`'s CPE last informed 96 hours ago. The tree is returned, because
  cached values are what an ACS actually holds, but `data_available` is False and the notes say the
  readings describe four-day-old conditions. Returning nothing would lose real evidence; returning
  it unflagged would let a detector date a verdict wrongly.
* **offline** -- `SVC-UT-001-B-01` and the dying-gasp ONT on `SVC-VQ-002-A-01`. No tree at all.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.domain.enums import ActionType
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError, AdapterUnavailableError
from lpr_cpe.security.redaction import mask_mac
from lpr_cpe.simulation.fixtures.determinism import jitter, pick, unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

#: Staleness at which a cached TR-181 tree stops being evidence about *now*. Twice the 07:00/21:00
#: scan interval, so a device that missed a single scan window is not called stale. The policy pack
#: owns the threshold detectors act on; this one only decides what this adapter says about itself.
STALE_AFTER_HOURS = 24.0

#: Diagnostics this simulator knows how to run. `run_diagnostic` refuses anything else rather than
#: returning a plausible pass, because a silent pass for a misspelled diagnostic is a test that
#: never ran and evidence that says it did.
SUPPORTED_DIAGNOSTICS = (
    "ip_ping",
    "traceroute",
    "download_speed",
    "upload_speed",
    "wifi_neighbour_scan",
    "dns_lookup",
)

#: Remote actions this adapter accepts. Anything else is refused -- policy decides *whether* an
#: action is allowed, but the adapter still has to know whether it is even implementable.
SUPPORTED_ACTIONS = frozenset(
    {
        ActionType.CPE_REBOOT,
        ActionType.CPE_RESYNC,
        ActionType.CPE_FIRMWARE_UPDATE,
        ActionType.CPE_FACTORY_RESET,
        ActionType.WIFI_CHANNEL_CHANGE,
        ActionType.WIFI_POWER_CHANGE,
        ActionType.PROFILE_CHANGE,
        ActionType.REPROVISION,
        ActionType.BULK_CONFIG_PUSH,
    }
)

_HOSTNAME_POOL = (
    "smart-tv",
    "laptop",
    "phone",
    "tablet",
    "console",
    "thermostat",
    "camera",
    "printer",
    "speaker",
    "watch",
)


class SimulatedCPEAdapter(SimulatedAdapterBase):
    """TR-069/TR-181 reads, service tests, and remote actions -- all against fixtures."""

    system_name = "cpe"
    external_ref_prefix = "ACS"

    # -- reads -----------------------------------------------------------------------------------

    async def read_status(self, cpe_ref: str) -> dict[str, Any]:
        """Device status and access-technology telemetry. **Subject read**: unknown ref raises.

        Shaped to populate `CPERecord` directly: the keys are that model's field names, so the
        mapping from adapter payload to domain object is a `CPERecord(**payload)`-shaped move with
        no per-field translation layer to drift. `radios` is deliberately absent -- it comes from
        `read_wifi_status`, which is the method that does the masking.
        """
        self._ensure_available()
        device = self._fixtures.cpe(cpe_ref, system=self.system_name)
        service = self._fixtures.service(str(device["service_ref"]), system=self.system_name)
        profile = self._fixtures.telemetry(service)
        offline = bool(device["offline"])
        inform_offset = float(device["last_inform_offset_hours"])
        stale = abs(inform_offset) >= STALE_AFTER_HOURS

        notes: list[str] = list(device["data_quality_notes"])
        if stale and not offline:
            notes.append(
                f"last inform {abs(inform_offset):.0f}h ago, beyond the {STALE_AFTER_HOURS:.0f}h "
                "freshness window: these are cached values"
            )

        payload: dict[str, Any] = {
            "cpe_ref": cpe_ref,
            "service_ref": device["service_ref"],
            "serial_number": device["serial_number"],
            "model": device["model"],
            "vendor": device["vendor"],
            "firmware_version": device["firmware_version"],
            "technology": device["technology"],
            "management_protocol": device["management_protocol"],
            "online": not offline,
            "last_inform_at": self._offset_hours(inform_offset),
            # Withheld rather than zeroed when the device is down: an uptime of 0 is a number a
            # detector would happily average.
            "uptime_seconds": None if offline else int(device["uptime_seconds"]),
            "data_available": not offline,
            "data_quality_notes": notes,
            **self._provenance(cpe_ref),
        }

        if offline:
            payload.update(
                {
                    "downstream_power_dbmv": None,
                    "upstream_power_dbmv": None,
                    "downstream_snr_db": None,
                    "uncorrectable_codewords": None,
                    "rx_optical_power_dbm": None,
                    "tx_optical_power_dbm": None,
                }
            )
            return payload

        if device["technology"] == "hfc":
            payload.update(
                {
                    "downstream_power_dbmv": round(
                        float(profile["downstream_power_dbmv"]) + jitter(cpe_ref, "ds", 0.8), 2
                    ),
                    "upstream_power_dbmv": round(
                        float(profile["upstream_power_dbmv"]) + jitter(cpe_ref, "us", 1.1), 2
                    ),
                    "downstream_snr_db": round(
                        float(profile["downstream_snr_db"]) + jitter(cpe_ref, "snr", 0.6), 2
                    ),
                    "uncorrectable_codewords": int(profile["uncorrectable_codewords"]),
                    "rx_optical_power_dbm": None,
                    "tx_optical_power_dbm": None,
                }
            )
        else:
            rx = profile["rx_optical_power_dbm"]
            tx = profile["tx_optical_power_dbm"]
            payload.update(
                {
                    "downstream_power_dbmv": None,
                    "upstream_power_dbmv": None,
                    "downstream_snr_db": None,
                    "uncorrectable_codewords": None,
                    "rx_optical_power_dbm": (
                        round(float(rx) + jitter(cpe_ref, "rx", 0.7), 2) if rx is not None else None
                    ),
                    "tx_optical_power_dbm": (
                        round(float(tx) + jitter(cpe_ref, "tx", 0.4), 2) if tx is not None else None
                    ),
                }
            )
        return payload

    async def read_wifi_status(self, cpe_ref: str) -> dict[str, Any]:
        """The TR-181 `Device.WiFi.*` subtree, with every client MAC masked. **Subject read**.

        Returns `Device.WiFi.Radio.*`, `Device.WiFi.SSID.*` and `Device.WiFi.AccessPoint.*`,
        including `AssociatedDevice.*` entries carrying per-client `SignalStrength`. The associated
        devices are the only place a client identifier could appear, and `mask_mac` has already run
        on every one by the time this returns.

        `data_available` is False in three distinguishable cases, and the notes say which: the
        device is offline, the tree is stale, or the firmware returned an empty Wi-Fi subtree.
        Collapsing those into "no data" would lose the difference between a broken device and a
        broken read.
        """
        self._ensure_available()
        device = self._fixtures.cpe(cpe_ref, system=self.system_name)
        offline = bool(device["offline"])
        inform_offset = float(device["last_inform_offset_hours"])
        stale = abs(inform_offset) >= STALE_AFTER_HOURS
        notes: list[str] = list(device["data_quality_notes"])

        if offline:
            # No tree at all. An empty-but-present tree would be read as "zero clients, no
            # utilisation, healthy", which is the most flattering possible reading of a dead device.
            return {
                "cpe_ref": cpe_ref,
                "service_ref": device["service_ref"],
                "last_inform_at": self._offset_hours(inform_offset),
                "inform_age_hours": round(abs(inform_offset), 2),
                "online": False,
                "parameters": {},
                "radios": [],
                "ssids": [],
                "access_points": [],
                "client_count": None,
                "data_available": False,
                "data_quality_notes": [
                    *notes,
                    "device offline: no TR-181 Device.WiFi.* subtree available",
                ],
                "masking_applied": True,
                "mask_owner": "lpr_cpe.security.redaction.mask_mac",
                **self._provenance(cpe_ref),
            }

        wifi = self._fixtures.wifi_profiles[str(device["wifi_profile"])]
        empty_subtree = wifi["utilization_2g_pct"] is None
        if empty_subtree:
            notes.append(
                "firmware returned an empty Device.WiFi.AccessPoint.*.AssociatedDevice.* subtree; "
                "client counts and RSSI are unavailable, not zero"
            )
        if stale:
            notes.append(
                f"TR-181 values cached {abs(inform_offset):.0f}h ago; any Wi-Fi verdict derived "
                "from them describes conditions at that time, not now"
            )

        radios = [
            self._radio(cpe_ref, device, wifi, band="2.4GHz", index=1),
            self._radio(cpe_ref, device, wifi, band="5GHz", index=2),
        ]
        ssids = [
            {
                "path": f"Device.WiFi.SSID.{i}",
                "Device.WiFi.SSID.Enable": True,
                "Device.WiFi.SSID.Status": "Up",
                "Device.WiFi.SSID.Name": f"wlan{i - 1}",
                "Device.WiFi.SSID.SSID": device["ssid_2g" if i == 1 else "ssid_5g"],
                "Device.WiFi.SSID.LowerLayers": f"Device.WiFi.Radio.{i}",
                # The gateway's own BSSID is masked on the same rule as a client MAC. It identifies
                # a household's AP, so treating it as "infrastructure, therefore fine" would leak
                # exactly the identifier the control exists to remove.
                "Device.WiFi.SSID.BSSID": mask_mac(self._synthetic_mac(cpe_ref, f"bssid{i}")),
            }
            for i in (1, 2)
        ]
        access_points = [
            self._access_point(cpe_ref, device, wifi, band="2.4GHz", index=1),
            self._access_point(cpe_ref, device, wifi, band="5GHz", index=2),
        ]
        clients_key = "Device.WiFi.AccessPoint.AssociatedDeviceNumberOfEntries"
        total_clients = (
            None if empty_subtree else sum(int(ap[clients_key]) for ap in access_points)
        )
        return {
            "cpe_ref": cpe_ref,
            "service_ref": device["service_ref"],
            "last_inform_at": self._offset_hours(inform_offset),
            "inform_age_hours": round(abs(inform_offset), 2),
            "online": True,
            # A flat parameter map alongside the structured lists. The reference pipeline flattens
            # the tree before scoring, and both shapes are useful: the flat map is what a real ACS
            # GetParameterValues returns, the lists are what the KPI extractor iterates.
            "parameters": {
                "Device.WiFi.RadioNumberOfEntries": len(radios),
                "Device.WiFi.SSIDNumberOfEntries": len(ssids),
                "Device.WiFi.AccessPointNumberOfEntries": len(access_points),
                "Device.DeviceInfo.SoftwareVersion": device["firmware_version"],
                "Device.DeviceInfo.ModelName": device["model"],
                "Device.DeviceInfo.Manufacturer": device["vendor"],
                "Device.DeviceInfo.UpTime": int(device["uptime_seconds"]),
            },
            "radios": radios,
            "ssids": ssids,
            "access_points": access_points,
            "client_count": total_clients,
            "wifi_profile": device["wifi_profile"],
            # False when stale or when the subtree is empty, so a detector cannot treat either as a
            # clean scan. The notes above say which of the two it was.
            "data_available": not (stale or empty_subtree),
            "data_quality_notes": notes,
            "masking_applied": True,
            "mask_owner": "lpr_cpe.security.redaction.mask_mac",
            **self._provenance(cpe_ref),
        }

    def _radio(
        self,
        cpe_ref: str,
        device: dict[str, Any],
        wifi: dict[str, Any],
        *,
        band: str,
        index: int,
    ) -> dict[str, Any]:
        """One `Device.WiFi.Radio.{i}` entry.

        The channel is chosen by `determinism.pick` from the profile's channel list, so a device is
        always on the same channel -- a channel-change action that appears to have done nothing
        because the "before" read drifted is an unfalsifiable action.
        """
        suffix = "2g" if band == "2.4GHz" else "5g"
        util = wifi[f"utilization_{suffix}_pct"]
        noise = wifi[f"noise_floor_{suffix}_dbm"]
        channels: tuple[int, ...] = tuple(wifi[f"channels_{suffix}"])
        return {
            "path": f"Device.WiFi.Radio.{index}",
            "Device.WiFi.Radio.Enable": True,
            "Device.WiFi.Radio.Status": "Up",
            "Device.WiFi.Radio.OperatingFrequencyBand": band,
            "Device.WiFi.Radio.Channel": pick(cpe_ref, f"chan{suffix}", channels),
            "Device.WiFi.Radio.AutoChannelEnable": suffix == "5g",
            "Device.WiFi.Radio.OperatingChannelBandwidth": "20MHz" if suffix == "2g" else "80MHz",
            "Device.WiFi.Radio.OperatingStandards": "b,g,n,ax" if suffix == "2g" else "a,n,ac,ax",
            "Device.WiFi.Radio.TransmitPower": 100,
            # Vendor-extension-shaped stats. TR-181 has `Device.WiFi.Radio.{i}.Stats.*` but the
            # utilisation and noise-floor members differ by vendor, so these names are ours.
            "Device.WiFi.Radio.Stats.ChannelUtilization": (
                None
                if util is None
                else round(float(util) + jitter(cpe_ref, f"util{suffix}", 3.0), 1)
            ),
            "Device.WiFi.Radio.Stats.NoiseFloor": (
                None
                if noise is None
                else round(float(noise) + jitter(cpe_ref, f"noise{suffix}", 1.5), 1)
            ),
            "Device.WiFi.Radio.Stats.ErrorsSent": (
                None if wifi["error_rate_pct"] is None else int(120 * float(wifi["error_rate_pct"]))
            ),
            "Device.WiFi.Radio.Stats.ErrorRatePct": wifi["error_rate_pct"],
        }

    def _access_point(
        self,
        cpe_ref: str,
        device: dict[str, Any],
        wifi: dict[str, Any],
        *,
        band: str,
        index: int,
    ) -> dict[str, Any]:
        """One `Device.WiFi.AccessPoint.{i}` entry with its associated devices, MACs masked."""
        suffix = "2g" if band == "2.4GHz" else "5g"
        clients = self._clients(cpe_ref, device, wifi, band=band)
        return {
            "path": f"Device.WiFi.AccessPoint.{index}",
            "Device.WiFi.AccessPoint.Enable": True,
            "Device.WiFi.AccessPoint.Status": "Enabled",
            "Device.WiFi.AccessPoint.SSIDReference": f"Device.WiFi.SSID.{index}",
            "Device.WiFi.AccessPoint.SSIDAdvertisementEnabled": True,
            "Device.WiFi.AccessPoint.Security.ModeEnabled": "WPA3-Personal",
            "Device.WiFi.AccessPoint.AssociatedDeviceNumberOfEntries": len(clients),
            "Device.WiFi.AccessPoint.AssociatedDevice": clients,
            "band": band,
            "throughput_mbps": (
                None
                if wifi["throughput_mbps"] is None
                else round(float(wifi["throughput_mbps"]) * (0.35 if suffix == "2g" else 1.0), 1)
            ),
        }

    def _clients(
        self,
        cpe_ref: str,
        device: dict[str, Any],
        wifi: dict[str, Any],
        *,
        band: str,
    ) -> list[dict[str, Any]]:
        """Associated-device entries for one band. **Every MAC is masked before it is returned.**

        Two sources, one masking rule. Most devices' clients are synthesised deterministically from
        the profile's client count; two fixtures carry an explicit `clients` list so the masking is
        exercised against fixture-supplied addresses too. Neither path can skip the mask, because
        both leave through this method.
        """
        suffix = "2g" if band == "2.4GHz" else "5g"
        count = int(wifi[f"client_count_{suffix}"])
        worst = wifi["worst_rssi_dbm"]
        best = wifi["best_rssi_dbm"]
        if count == 0 or worst is None or best is None:
            return []

        explicit = [c for c in device.get("clients", []) if c["band"] == band]
        out: list[dict[str, Any]] = []
        for i, client in enumerate(explicit[:count], start=1):
            out.append(
                self._associated_device(
                    index=i,
                    mac=str(client["mac"]),
                    rssi=float(client["rssi_dbm"]),
                    hostname=str(client["hostname"]),
                    cpe_ref=cpe_ref,
                    salt=f"{suffix}exp{i}",
                )
            )
        # Spread the remaining clients across [worst, best] so the RSSI summary the KPI extractor
        # computes is ordered worst <= avg <= best -- which `WifiRadioSnapshot` validates.
        remaining = count - len(out)
        for i in range(1, remaining + 1):
            index = len(out) + i
            fraction = (i - 0.5) / remaining if remaining > 1 else 0.5
            rssi = float(worst) + (float(best) - float(worst)) * fraction
            out.append(
                self._associated_device(
                    index=index,
                    mac=self._synthetic_mac(cpe_ref, f"{suffix}{index}"),
                    rssi=round(rssi, 1),
                    hostname=_HOSTNAME_POOL[
                        int(unit(cpe_ref, f"host{suffix}{index}") * len(_HOSTNAME_POOL))
                        % len(_HOSTNAME_POOL)
                    ],
                    cpe_ref=cpe_ref,
                    salt=f"{suffix}{index}",
                )
            )
        return out

    def _associated_device(
        self,
        *,
        index: int,
        mac: str,
        rssi: float,
        hostname: str,
        cpe_ref: str,
        salt: str,
    ) -> dict[str, Any]:
        return {
            "path": f"Device.WiFi.AccessPoint.AssociatedDevice.{index}",
            # The masked value, and the only form of the address this method ever emits.
            "Device.WiFi.AccessPoint.AssociatedDevice.MACAddress": mask_mac(mac),
            "Device.WiFi.AccessPoint.AssociatedDevice.SignalStrength": rssi,
            "Device.WiFi.AccessPoint.AssociatedDevice.Active": True,
            "Device.WiFi.AccessPoint.AssociatedDevice.LastDataDownlinkRate": int(
                160_000 + unit(cpe_ref, f"dl{salt}") * 700_000
            ),
            "Device.WiFi.AccessPoint.AssociatedDevice.LastDataUplinkRate": int(
                60_000 + unit(cpe_ref, f"ul{salt}") * 300_000
            ),
            "Device.WiFi.AccessPoint.AssociatedDevice.Retransmissions": int(
                unit(cpe_ref, f"retx{salt}") * 400
            ),
            # A device *class*, not a name: "smart-tv" is diagnostically useful (a TV at -84 dBm
            # explains a streaming complaint) and identifies nobody. A real ACS returns the
            # client-supplied hostname, which often contains a person's name, so the real
            # integration has to mask this field too -- gap CPE-4.
            "device_class": hostname,
        }

    def _synthetic_mac(self, cpe_ref: str, salt: str) -> str:
        """A stable locally-administered MAC for a synthetic client.

        `02:` prefix: the second-least-significant bit of the first octet marks a
        locally-administered address, so these cannot collide with a real vendor OUI. It is still
        masked before it leaves -- the point of the mask is the rule, not the value.
        """
        raw = int(unit(cpe_ref, f"mac{salt}") * 0xFFFFFFFF)
        octets = [0x02, 0x1F, (raw >> 24) & 0xFF, (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF]
        return ":".join(f"{o:02X}" for o in octets)

    async def run_diagnostic(self, cpe_ref: str, diagnostic: str) -> dict[str, Any]:
        """Run one service test. **Subject read**: unknown CPE raises.

        An unsupported diagnostic name raises a **non-retryable** `AdapterError`. Retrying a
        misspelled diagnostic three times is three ways to get the same answer, and `with_retry`
        already honours `retryable=False` for exactly this.
        """
        self._ensure_available()
        device = self._fixtures.cpe(cpe_ref, system=self.system_name)
        if diagnostic not in SUPPORTED_DIAGNOSTICS:
            raise AdapterError(
                self.system_name,
                f"unsupported diagnostic {diagnostic!r}; known: {', '.join(SUPPORTED_DIAGNOSTICS)}",
                retryable=False,
            )
        if bool(device["offline"]):
            raise AdapterUnavailableError(
                self.system_name, f"{cpe_ref} is offline; {diagnostic} cannot be run"
            )
        service = self._fixtures.service(str(device["service_ref"]), system=self.system_name)
        impaired = str(service["health"]) not in {"hfc_healthy", "pon_healthy"}
        seed = f"{cpe_ref}:{diagnostic}"
        speed_ceiling = float(service["downstream_speed_mbps"])

        results: dict[str, dict[str, Any]] = {
            "ip_ping": {
                "success_count": 4 if impaired else 5,
                "failure_count": 1 if impaired else 0,
                "average_response_time_ms": round(
                    (38.0 if impaired else 11.0) + jitter(seed, "rtt", 3.0), 1
                ),
                "maximum_response_time_ms": round(
                    (210.0 if impaired else 24.0) + jitter(seed, "rttmax", 8.0), 1
                ),
                "packet_loss_pct": 20.0 if impaired else 0.0,
            },
            "traceroute": {
                "hop_count": 9 + int(unit(seed, "hops") * 4),
                "response_time_ms": round(
                    (44.0 if impaired else 14.0) + jitter(seed, "tr", 4.0), 1
                ),
            },
            "download_speed": {
                # Throughput as a fraction of the *sold* rate, so a 100 Mbps product is not judged
                # against a 1 Gbps expectation. A raw Mbps threshold would fail every entry tier.
                "throughput_mbps": round(speed_ceiling * (0.31 if impaired else 0.94), 1),
                "sold_mbps": speed_ceiling,
                "fraction_of_sold": 0.31 if impaired else 0.94,
            },
            "upload_speed": {
                "throughput_mbps": round(speed_ceiling * 0.1 * (0.28 if impaired else 0.92), 1),
                "sold_mbps": round(speed_ceiling * 0.1, 1),
                "fraction_of_sold": 0.28 if impaired else 0.92,
            },
            "wifi_neighbour_scan": {
                "neighbour_ap_count": 4 + int(unit(seed, "neigh") * 22),
                "strongest_neighbour_rssi_dbm": round(-58.0 + jitter(seed, "nrssi", 9.0), 1),
                "co_channel_ap_count": 1 + int(unit(seed, "cochan") * 9),
            },
            "dns_lookup": {
                "resolved": True,
                "response_time_ms": round(21.0 + jitter(seed, "dns", 6.0), 1),
            },
        }
        return {
            "cpe_ref": cpe_ref,
            "diagnostic": diagnostic,
            # TR-069 diagnostics are asynchronous in reality: the ACS sets a state and the device
            # informs when complete. The simulator returns terminal state directly, which is a
            # shortcut the real adapter cannot take -- gap CPE-5.
            "status": "Complete",
            "result": results[diagnostic],
            "started_at": self._offset_hours(-0.02),
            "completed_at": self._clock.now().isoformat(),
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(cpe_ref),
        }

    # -- write -----------------------------------------------------------------------------------

    async def apply_action(self, request: ActionRequest) -> dict[str, Any]:
        """Apply an approved remote action. Goes through the gate; never performs I/O.

        Records the *intended* parameter writes on the result, because in simulation the intent is
        the only artefact -- and a reviewer asking "what would this reboot actually have sent" needs
        an answer that is not "read the adapter source".
        """
        if request.action_type not in SUPPORTED_ACTIONS:
            raise AdapterError(
                self.system_name,
                f"{request.action_type.value} is not a CPE action; "
                f"known: {', '.join(sorted(a.value for a in SUPPORTED_ACTIONS))}",
                retryable=False,
            )
        # Confirm the target exists before claiming an effect on it. A simulated reboot of a CPE
        # that is not in inventory is a successful-looking action against nothing.
        self._fixtures.cpe(request.target_ref, system=self.system_name)
        return self.simulate_write(
            request,
            detail=(
                f"{request.action_type.value} recorded for {request.target_ref}; "
                "no TR-069 session opened (fixture-backed simulator)"
            ),
            extra={
                "intended_parameters": self._intended_parameters(request),
                "reversible": request.reversible,
            },
        )

    def _intended_parameters(self, request: ActionRequest) -> dict[str, Any]:
        """The TR-181 parameters or RPC this action would have set.

        Paths are real TR-181; the mapping from our `ActionType` to them is our design decision and
        an integration-discovery item (gap CPE-3) -- a firmware update in particular is a vendor
        RPC, not a parameter write, on most real platforms.
        """
        params = request.parameters
        match request.action_type:
            case ActionType.CPE_REBOOT:
                return {"rpc": "Reboot", "command_key": request.idempotency_key[:32]}
            case ActionType.CPE_FACTORY_RESET:
                return {"rpc": "FactoryReset", "command_key": request.idempotency_key[:32]}
            case ActionType.CPE_RESYNC:
                return {"rpc": "Inform", "event_code": "6 CONNECTION REQUEST"}
            case ActionType.CPE_FIRMWARE_UPDATE:
                return {
                    "rpc": "Download",
                    "file_type": "1 Firmware Upgrade Image",
                    "target_version": params.get("target_version"),
                }
            case ActionType.WIFI_CHANNEL_CHANGE:
                radio = params.get("radio_index", 1)
                return {
                    f"Device.WiFi.Radio.{radio}.Channel": params.get("channel"),
                    f"Device.WiFi.Radio.{radio}.AutoChannelEnable": False,
                }
            case ActionType.WIFI_POWER_CHANGE:
                radio = params.get("radio_index", 1)
                return {f"Device.WiFi.Radio.{radio}.TransmitPower": params.get("transmit_power")}
            case _:
                return {"rpc": "SetParameterValues", "parameters": dict(params)}
