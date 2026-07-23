# bv-config(1)

## NAME

`bv-config` - create or edit a BlackVue camera's configuration

## SYNOPSIS

```
bv-config [--config-dir DIR] ID
```

## DESCRIPTION

`bv-config` creates or edits a camera's configuration: its display **name**, one or more **endpoints** (network addresses the camera is reachable at, tried in order), and the **target** directory downloads are saved to.

It's the first command to run for a new camera - `bv-download`, `bv-ls`, `bv-generate`, and `bv-export` all read the archive this config points at, but only `bv-config` itself needs the camera's network address.

Running `bv-config` again on an existing `ID` edits it interactively, defaulting every question to the value already saved. Nothing is overwritten until the wizard finishes and you confirm.

The wizard asks, in order:

1. **Name** - a free-form display name (may contain UTF-8/emoji). Must pass validation (see `validate_name`).
2. **Target** - the local directory recordings are downloaded into. Must not be empty.
3. **Endpoints** - reviewed one at a time if editing an existing config (Enter keeps the current address, typing `remove` drops it), then new endpoints can be appended by address until you leave one blank to stop. Endpoints are tried in the order given here, so put the most reliable/fastest one first (e.g. a local Wi-Fi hotspot before a cloud relay).

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id - an ASCII alphanumeric string, max 128 characters, used everywhere else on the command line (`bv-download ID`, etc.) and as the config's own filename. Distinct from the free-form display **Name** asked by the wizard. |

## OPTIONS

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in. Default: the platform's standard config directory (e.g. `~/.config/beyond-video` on Linux). |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | Config saved successfully. |
| 1 | `ID` failed validation (not ASCII alphanumeric, too long, etc.) |
| 2 | Config file exists but couldn't be read/parsed. |

## EXAMPLES

Create a new camera config, interactively:

```
bv-config Kirby
```

Edit an existing one (every prompt defaults to the saved value):

```
bv-config Kirby
```

Use a non-default config directory (useful for testing, or keeping multiple independent setups):

```
bv-config Kirby --config-dir ./test-configs
```

## FILES

Configs are saved as TOML under `--config-dir`, one file per camera `ID`.

## SEE ALSO

`bv-download(1)`, the first command that reads a config this creates.
