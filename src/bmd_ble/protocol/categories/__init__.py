"""
bmd_ble/protocol/categories
============================
One module per BMD command category (recording, settings, media, metadata).

Each module encodes/decodes packets for its command family only. Category
and parameter byte values are model/firmware-specific and must be supplied
by the caller from a sniffer-verified ``CameraProfile`` — never hardcoded
here. See CLAUDE.md, "Command categories" table and design principle 6
(sniffer-first for all protocol values).
"""
