# Monitor

## Purpose

The monitor commands display the current state of a camera system.

Two commands are provided:

```text
bv-status

bv-monitor
```

---

# Status

`bv-status` displays a single snapshot of the camera state and exits.

Example:

```text
$ bv-status Kirby

Model       : DR970X
Recording   : Yes
GPS         : Locked
Wi-Fi       : Connected
Temperature : 48 °C
Battery     : 12.4 V
```

This command is intended for scripts and one-time checks.

---

# Monitor

`bv-monitor` continuously displays the current camera state until interrupted.

Example:

```text
$ bv-monitor Kirby
```

---

# Output modes

`bv-monitor` automatically selects its output mode depending on whether stdout is connected to a terminal.

## Interactive terminal

Dashboard mode.

The display is continuously updated.

Example:

```text
Camera      : Kirby
Recording   : Yes
GPS         : 73 km/h
Heading     : 182°
Wi-Fi       : Connected
Temperature : 48 °C

Last update : 12:34:52
```

The display should be refreshed in place rather than scrolling.

---

## Redirected or piped output

Log mode.

One line is written for each state change.

Example:

```text
12:34:01  Recording Started
12:34:17  GPS : 73 km/h
12:34:28  Wi-Fi Connected
12:35:02  GPS Lost
12:35:11  Recording Stopped
```

Unchanged values should not be written.

This makes the output suitable for:

```text
> logfile

tee

systemd

cron

other logging systems
```

---

# Output mode overrides

The automatic mode may be overridden.

```text
--dashboard

--log
```

Examples:

```text
bv-monitor Kirby --dashboard

bv-monitor Kirby --log
```

---

# Refresh interval

The default refresh interval is one second.

It may be changed using:

```text
--interval SECONDS
```

Example:

```text
bv-monitor Kirby --interval 5
```

---

# Future extensions

Possible future additions include:

```text
--json

--gps

--network

--gsensor
```

These options are intentionally left for future development.

---

# Design principles

- `bv-status` performs a single status request.
- `bv-monitor` repeatedly requests camera status.
- Interactive terminals display a dashboard.
- Redirected output becomes a chronological log.
- Log output records only changes.
- Dashboard output is refreshed in place.
- The monitor is read-only and never modifies the camera.
