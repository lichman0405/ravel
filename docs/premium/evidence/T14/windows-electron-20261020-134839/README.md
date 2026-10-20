# Incomplete packaged-launch attempt

The unpacked Electron phase completed through `07-login-after-logout.png`. The
driver then tried to attach Playwright's Electron inspector to the NSIS portable
wrapper with `_electron.launch`. The wrapper extracts the application and starts
it as a separate process, so the inspector handshake never completed; the launch
timed out after 120 seconds and the driver exited without writing `result.json`.
The screenshots in this directory are the raw artifacts of that failed attempt.

This run is superseded by `windows-electron-20261020-135449`, which launches the
portable executable as a real process, verifies its top-level Windows window,
and drives the packaged renderer through the DevTools endpoint that the packaged
application itself exposes.
