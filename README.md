# monitors-setup

Configuration-driven NVIDIA Xorg virtual monitors for a headless Linux host.
The tool manages only the local `:0` NVIDIA Xorg configuration.

## Configure

Edit `config.toml`. Each `[[monitors]]` entry defines:

- `name`: 1-12 printable ASCII characters; stored as the EDID product name.
- `connector`: NVIDIA Xorg display-device name such as `DFP-1`.
- `primary`: whether this is the preferred Xinerama display.
- `default_mode`: startup resolution and refresh preference.
- `modes`: all advertised `WIDTHxHEIGHT@HZ` modes.

### Choosing connector values

`DFP-*` names are assigned by the NVIDIA Xorg driver and depend on the GPU and
its output topology. They are also different from the `DP-*` and `HDMI-*`
names shown by `xrandr`.

With the target X server running, list its display devices and aliases:

```bash
sudo env DISPLAY=:0 \
  XAUTHORITY=/var/run/lightdm/root/:0 \
  nvidia-settings -q dpys
```

Each device in the output has a `Has the following names` block. Use the
`DFP-N` alias from that block as the `connector` value. For example, a block
containing both `DP-0` and `DFP-1` means that Xorg configuration must use
`DFP-1`, not `DP-0`.

The Xorg log provides a second way to list the names supported by the GPU and
inspect their connection type and maximum pixel clock:

```bash
sudo grep -A 10 'Valid display device(s)' /var/log/Xorg.0.log
sudo grep -E 'DFP-[0-9]+: (Internal|[0-9.]+ MHz maximum)' \
  /var/log/Xorg.0.log
```

Choose one distinct `DFP-N` value for each virtual monitor. The same device
block also contains a `Connector-N` alias; choose devices with different
`Connector-N` values so that two virtual monitors do not use alternate
signaling paths of the same physical connector. For high-bandwidth modes such
as 4K at 120 Hz, prefer entries reported as `Internal DisplayPort` with a
sufficiently high maximum pixel clock.

On the host for which the example `config.toml` was created, `DFP-1` maps to
`DP-0` and `DFP-3` maps to `DP-2`; both are native DisplayPort paths. Re-run
the queries above after moving the configuration to another GPU or host.

Monitors are arranged left-to-right in configuration order. After changing the
file, run the installer again. Repeated installs preserve the backup from the
first install and replace only files managed by this tool.

## Commands

```bash
# Render and validate locally without changing the system
python3 monitors_setup.py render

# Install or update, restart LightDM, and verify every configured mode
./install.sh

# Show installed and live status
sudo ./status.sh

# Restore the Xorg configuration that existed before the first install
./uninstall.sh
```

Use `--no-restart` with install/uninstall to defer restarting LightDM. If the
managed Xorg file was edited manually, install/uninstall refuses to overwrite
it; inspect the change and then use `--force` if appropriate.

To use another configuration without editing the scripts:

```bash
MONITORS_SETUP_CONFIG=/absolute/path/other.toml ./install.sh
```

Installation state and the original Xorg backup are kept under
`/var/lib/monitors-setup`. EDIDs are installed under
`/etc/X11/edid/monitors-setup`.
