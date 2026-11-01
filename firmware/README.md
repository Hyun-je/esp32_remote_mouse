# BLE HID firmware

ESP32-C3 USB serial to BLE HID absolute mouse and keyboard bridge, used by the desktop application.

Build: `pio run -d firmware`

After flashing and pairing `ESP32 Remote Input`, open the serial monitor at 115200 baud. Commands end in a newline:

- `status`: print connection status
- `goto 5000 5000`: move the absolute pointer, normalized coordinates 0..10000
- `prepare 5000 5000`: stabilize the absolute pointer position before a click
- `down 5000 5000`: press the pointer button at screen center
- `down 5000 5000 2`: press the right pointer button
- `move 7000 5000`: move with the button held
- `up`: release the pointer button
- `scroll 1 1` / `scroll 0 1`: scroll one step up / down
- `key 0 4`: press HID keyboard usage 4 (A) without modifiers
- `key 0 0`: release keys
- `keys 2 4 5`: hold left Shift and HID usages 4 and 5 simultaneously (up to six keys); `keys 0` releases all keys. Each command replaces the entire keyboard state.
- `release`: release the pointer button and keys
- `sleep`: release inputs, disconnect the target, stop advertising and lower the CPU clock; prints `SLEEPING`
- `wake`: restore the CPU clock and resume advertising so the bonded target can reconnect; prints `AWAKE`. `ble_recover` also wakes.

Only send test input while the target device is ready to receive it. On one iPad with AssistiveTouch enabled, BLE keyboard input, absolute pointer movement, and a drag drawing a line in Notes were observed. iPad model, OS version, click accuracy, reconnect behavior, and other hosts remain unverified. A successful serial `OK` confirms command handling, not that the host accepted or acted on a report. `move` requires a preceding `down`; `up` and `release` clear the button. On each BLE reconnect, the firmware sends a neutral keyboard and pointer state once both reports are subscribed. While a key or button is held, send `status` or another command at least every 2.5 seconds: the firmware releases inputs on timeout and emits `INPUT_TIMEOUT`. The desktop app polls every second.
