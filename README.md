# X-T30 III Tether Enabler

A local Mac/Linux utility that exposes **USB Fixed** on the **Fujifilm X-T30 III, firmware 1.10**, then checks whether the camera retains tethering after a normal restart.

The development camera successfully used FUJIFILM TETHER APP, retained USB Fixed across a normal power-off/on, and transferred a new JPEG+RAW pair afterward without another service-mode setup. The menu patch is temporary; the selected connection setting is saved through the camera's native menu.

## Run

1. Clone or download this repository into a writable folder. Install `uv` and `libusb`. On macOS with Homebrew, run `brew install uv libusb` or open `enabler/Install Mac dependencies.command`.
2. If you need to expose the menu, supply your own **unmodified X-T30 III 1.10 `FWUP0037.DAT`** and import its reference images locally:

   ```sh
   uv run enabler/import_firmware.py /path/to/FWUP0037.DAT
   ```

3. Start the utility:

   ```sh
   ./enabler/run.sh
   ```

   On Mac, you can instead open **enabler/Start Tether Enabler.command**. The browser opens <http://127.0.0.1:8769/>; keep the terminal running.

An already-enabled camera skips setup and does not need the firmware references for its read-only check. Close other camera applications before using this utility. After verification, close the utility and open FUJIFILM TETHER APP or your existing photo receiver.

## What the guide does

1. Checks the camera identity and current USB mode.
2. If needed, walks you through selecting **USB Card Reader**, then starting service mode with **Shutter + joystick Up** held during startup.
3. Verifies original camera bytes, tests the menu patch and restores that test, then temporarily exposes **USB Auto** and **USB Fixed**.
4. Guides you through pressing **PLAY**, unplugging USB without powering off, and selecting **USB Fixed** in the camera menu.
5. Verifies USB Fixed after a user-confirmed normal restart.

It does not flash firmware, trigger the shutter, or receive photographs. Setup is restricted to the exact checked camera/firmware. A failed or uncertain operation stops and retains its recovery journal.

## Current limits

- Keep USB Fixed selected. Permanent menu visibility, battery removal, and switching away from the hidden setting have not been validated.
- The temporary menu replaces Webcam and RAW conversion rows with Auto and Fixed.
- The underlying procedure has been tested on the development camera; the complete new GUI workflow still needs a fresh hardware walkthrough. Browser and automated workflow tests are separate from hardware validation.
- Only X-T30 III firmware **1.10** is supported. The original DAT must have SHA-256 `8a32087606b1ded920458042528054b3b0416fbb04873e6840cfde031abd0d79`.
- The repository contains source and a minimal static execution-proof record. Firmware binaries, photographs, private camera-session journals, and device configuration dumps are not included. Importing references never contacts a camera.

The guide saves its progress in `enabler-data/` and camera-operation journals in `analysis/ff80-menu-*.json`. These are gitignored, but keep them locally: future setup attempts use them to verify recovery. Avoid running copies of the utility from different directories against an active setup.

## Development

```sh
uv run --no-project --with pyusb==1.3.1 --with libusb1==3.3.1 \
  python -m unittest discover -s enabler -p 'test_*.py' -v
./enabler/run.sh --demo --port 0
```

The original-byte baseline tests run when the checked reference images are available; otherwise they are explicitly skipped. Other tests use synthetic USB data and do not need a camera. Use `--no-browser` to suppress browser launch or `--port 0` to choose an available port.

See [third-party notices](THIRD_PARTY_NOTICES.md) for the bundled service transport code. This project is not affiliated with Fujifilm.
