# Device inventory

The dashboard discovers cameras, plugs, and lights at runtime. Do not commit device ids, account hashes, or LAN addresses.

- Set `TAPO_HUB_IP` to the H500 address. See [INSTALL.md](INSTALL.md).
- Plugs and lights on another subnet go in `extra_devices.yaml`, copied from `devices.yaml.example`.
- Battery cameras on an H500 do not have their own LAN addresses. Live view uses the hub address plus `deviceId`.

## Notes from local testing

- `get_general_device_list()` can be empty on an H500.
- `get_child_device_list()` can fail to parse. `get_child_device_list_json()` is the list the dashboard prefers.
- A rejected password is not retried. A locked hub refuses further logins for a while.
