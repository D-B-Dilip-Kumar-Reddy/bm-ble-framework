"""
bmd_camera/rest/constants.py
=============================
REST/WebSocket transport-level constants for the Blackmagic Camera 8.6
control API.

WHAT BELONGS HERE
──────────────────
Only values fixed by the published API spec (the uploaded OpenAPI/AsyncAPI
YAMLs) or by this camera's own confirmed serving behaviour, and that apply
regardless of model/firmware:
  • The API base path and WebSocket path (`Notification.yaml`'s `servers.url`)
  • Default timeouts

WHAT DOES NOT BELONG HERE
───────────────────────────
  • Per-endpoint availability (`501` vs `204`) — lives in
    payloads/models/<MODEL>/rest/<FW>.json, populated from
    tools/rest/probe_endpoints.py sweep output (design principle 6's REST
    sibling — see docs/rest/transport.md)
  • Codec/format name strings — live in the same profile's `format_names`
  • The default scheme/host — the camera's address and which scheme it
    serves (Setup -> Network Access) vary per network and are never fixed;
    see docs/rest/transport.md
"""

API_BASE = "/control/api/v1"
WS_PATH = "/control/api/v1/event/websocket"
MOUNTS_PATH = "/mounts/"

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_WS_TIMEOUT_S = 5.0
