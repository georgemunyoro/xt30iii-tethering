# X-T30 III Tether Enabler

A local utility for macOS, Linux, and Windows x64 that exposes **USB Fixed** on the **Fujifilm X-T30 III, firmware 1.10**, then checks whether the camera retains tethering after a normal restart. Windows support is experimental until a complete camera walkthrough is confirmed.

The development camera successfully used FUJIFILM TETHER APP, retained USB Fixed across a normal power-off/on, and transferred a new JPEG+RAW pair afterward without another service-mode setup. The menu patch is temporary; the selected connection setting is saved through the camera's native menu.

## Run

For Windows, use the [Windows setup](#windows-setup) below.

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

### macOS connection troubleshooting

If an older build reports **“Interface has an active kernel driver; it was left attached”**, update this repository and restart the utility. The macOS connection check now attempts the ordinary exclusive interface claim instead of rejecting the connection based on that driver indication. It does not detach drivers or reset the camera.

If the interface claim is denied, the utility offers **Release Mac camera and retry**. Finish all photo transfers and quit camera applications before clicking it. This force-quits recognized Apple camera helpers (`PTPCamera`, `ptpcamerad`, `mscamerad`, `mscamerad-xpc`) owned by your login once, then immediately checks the camera. It only targets system executables, rechecks their identity before signaling, and records the attempt in the connection report. It does not run a background kill loop or disable macOS services. If it still fails, expand **Connection details** and send the release results; macOS may have restarted a helper or another application may own the interface.

If the actual claim reports **Resource busy** or **Access denied**, quit FUJIFILM TETHER APP, Photos, Image Capture, and any other camera utilities. After any photo transfer finishes, Apple's background camera helper can also be stopped for the current session with `pkill -x 'PTPCamera|ptpcamerad'`, then immediately click **Check connection** without reconnecting USB. In Image Capture, choosing **Connecting this camera opens → No application** can prevent an importing app from reopening. Unlock the Mac and allow the USB accessory if prompted when connecting it. See [Apple's accessory instructions](https://support.apple.com/en-us/102282). Copy the complete **Details:** text if the check still fails.

On macOS, **Access denied (insufficient permissions)** can also mean another process owns the interface; [libusb maps exclusive-access conflicts to this error](https://github.com/libusb/libusb/blob/v1.0.30/libusb/os/darwin_usb.c). Some macOS versions use the helper name `ptpcamerad`, so stopping only `PTPCamera` may do nothing. If the check still fails, close the utility's terminal process, leave the camera connected, and run this from the repository folder:

```sh
./enabler/run.sh --diagnose-usb
```

Send the terminal output, including any `libusb:` lines. This probe records the OS and USB library, known camera helpers before/after the attempt, and the USB access stage. It only attempts to claim and release one normal Fujifilm PTP interface; it sends no PTP commands and does not stop helpers, detach drivers, or change camera settings. The JSON report is saved under `enabler-data/diagnostics/`. A helper appearing in the report is a clue, not proof that it owns the camera.

## Windows setup

Use **64-bit Windows 10 or 11 on an Intel/AMD PC**. Windows on ARM has not been validated. Extract the repository into a writable local folder (NTFS), rather than running inside the downloaded ZIP or from a network share.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) in PowerShell:

   ```powershell
   winget install --id astral-sh.uv -e
   ```

   Reopen your terminal after installation. The launcher installs Python 3.12 and the pinned Python dependencies automatically when needed.
2. Install **UsbDk_1.0.22_x64.msi** from the [official UsbDk release](https://github.com/daynix/UsbDk/releases/tag/v1.00-22). This installs a Windows system driver and requires administrator approval. Restart Windows if the installer requests it. The utility does not install drivers automatically.
3. If menu setup is needed, drag your original X-T30 III 1.10 **FWUP0037.DAT** onto **enabler/Import Firmware.cmd**. Alternatively, from the repository folder:

   ```powershell
   uv run --python 3.12 enabler/import_firmware.py 'C:\path\to\FWUP0037.DAT'
   ```

4. Quit other camera applications, then double-click **enabler/Start Tether Enabler.cmd**. Keep its terminal open while following the browser guide. To preview the interface without accessing USB:

   ```powershell
   & '.\enabler\Start Tether Enabler.cmd' --demo
   ```

5. For restart verification, leave the utility running and disconnect USB for **at least five seconds**, then switch the camera off/on normally and reconnect. Click **Verify restart**. Windows can reuse a USB address, so the guide also records observed disconnections; restarting the utility itself does not count.
6. Close the utility with **Ctrl+C** in its terminal before opening the Fuji app. If Windows has not rediscovered the camera, unplug and reconnect USB while keeping the camera powered on.

Windows uses [libusb's UsbDk backend](https://github.com/libusb/libusb/wiki/Windows). UsbDk temporarily redirects the camera while the utility owns it and releases it afterward. **Keep the existing Fuji/Windows camera driver; do not replace it with Zadig for this workflow.** The Python DLL is supplied by `libusb-package`; copying DLLs into system folders is unnecessary. If the guide reports missing UsbDk, check that the x64 runtime installed successfully and restart the utility. If using a VM, pass the camera through in both normal and service modes; it changes USB product ID during setup.

To check the Windows libraries and UsbDk runtime without opening a camera, run:

```powershell
& '.\enabler\Start Tether Enabler.cmd' --check-usb
```

Automated Windows tests cover startup, journal writes, process locking, library loading, and simulated workflow/recovery. They do not establish that USB transactions with a real camera succeed on Windows. That final hardware check is still pending.

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

The original-byte baseline tests run when the checked reference images are available; otherwise they are explicitly skipped. Other tests use synthetic USB data and do not need a camera. CI runs on Linux, macOS, and Windows. Use `--no-browser` to suppress browser launch or `--port 0` to choose an available port.

Project code is licensed under the [MIT License](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for the bundled service transport code and its original notices. This project is not affiliated with Fujifilm.
