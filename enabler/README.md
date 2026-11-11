# Fuji Tether Enabler

See the [project README](../README.md) for installation, firmware import, guided setup, validation, and limits.

Launch with `./enabler/run.sh` from the project folder, or open **Start Tether Enabler.command** on macOS. Install dependencies with **Install Mac dependencies.command** if needed. Keep the terminal open while using the browser at <http://127.0.0.1:8769/>.

This source repository contains the connection enabler. It does not include the separate photo receiver. Close the enabler before opening FUJIFILM TETHER APP or your existing receiver.

If your camera already reports USB Fixed, setup is skipped. Otherwise supply your own unchanged X-T30 III 1.10 `FWUP0037.DAT` using `uv run enabler/import_firmware.py /path/to/FWUP0037.DAT`, then follow the guide.
