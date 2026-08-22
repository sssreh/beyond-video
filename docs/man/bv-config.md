# bv-config(1)

## NAME

`bv-config` - create or edit a BlackVue camera's configuration

## SYNOPSIS

```
bv-config [--config-dir DIR] ID
```

## DESCRIPTION

`bv-config` creates or edits a camera's configuration: its display **name**, one or more **endpoints** (network addresses the camera is reachable at, tried in order), the **archive** directory downloads are saved to, and an optional **target** directory for `bv-export`.

It's the first command to run for a new camera - `bv-download`, `bv-ls`, `bv-generate`, `bv-export`, `bv-scribe`, and `bv-search` all read the archive this config points at (each also accepts `ID` itself in place of a literal archive path - see `docs/CLI.md`), but only `bv-config` itself needs the camera's network address.

Running `bv-config` again on an existing `ID` edits it interactively, defaulting every question to the value already saved. Nothing is overwritten until the wizard finishes and you confirm.

The wizard asks, in order:

1. **Name** - a free-form display name (may contain UTF-8/emoji). Must pass validation (see `validate_name`).
2. **Archive** - the local directory recordings are downloaded into (`bv-download`'s destination, and what every other `bv-*` command reads when you give them this camera's `ID` in place of a path - see `docs/CLI.md`). Must not be empty. For a brand-new camera, pre-filled with a suggested default (`~/beyond-video/archive/<ID>`, one subfolder per camera id so multiple cameras never collide) - press Enter to accept it, or type a different path.
3. **Target** - optional local directory `bv-export` writes trip folders into by default when its own `--target` flag isn't given explicitly (they share the name deliberately: this field *is* that flag's default value). Always pre-filled with a suggestion parallel to Archive - whatever you just answered for Archive, with its `archive` path component swapped for `trips` (e.g. `.../archive/Kirby` suggests `.../trips/Kirby`; `/data/archive` suggests `/data/trips`), or a plain `trips` folder next to Archive if it doesn't contain an `archive` component at all. Leave blank to require `--target` explicitly on every `bv-export` run (the behavior before this existed).
4. **Endpoints** - reviewed one at a time if editing an existing config (Enter keeps the current address, typing `remove` drops it), then new endpoints can be appended by address until you leave one blank to stop. Endpoints are tried in the order given here, so put the most reliable/fastest one first (e.g. a local Wi-Fi hotspot before a cloud relay).

**Naming note.** Earlier versions of this wizard called the Archive field "Target" and the Target field "Output" - both renamed for clarity, since `bv-download`/`bv-export` each already have their own separate `--target` flag with a different meaning, and re-using that word for the download directory too was confusing. Existing `.cfg` files from before this renaming used `target =`/`output =` as their TOML keys; running `bv-config` again on an old config migrates it to the new `archive =`/`target =` keys automatically the next time it's saved.

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id - ASCII alphanumeric plus `_`/`-`, max 128 characters, used everywhere else on the command line (`bv-download ID`, etc.) and as the config's own filename. Distinct from the free-form display **Name** asked by the wizard. |

## OPTIONS

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in. Default: `~/beyond-video-data/.config` (if you're upgrading from an older version, the old `~/.config/beyond-video` folder is moved there automatically the first time any `bv-*` command runs). |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | Config saved successfully. |
| 1 | `ID` failed validation (not ASCII alphanumeric/`_`/`-`, too long, etc.) |
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

An optional `notify.toml` in the same directory turns on email
notification when a `bv-*` command crashes while running unattended
(no terminal attached - cron, a scheduled task, a closed SSH session).
Global, not per-camera - not written by `bv-config`'s own wizard, just
hand-edit it directly:

```
email = "you@example.com"
smtp_host = "smtp.example.com"
smtp_port = 587          # optional, default 587
smtp_username = "..."    # optional - omit for an open/internal relay
smtp_password = "..."    # optional
use_tls = true            # optional, default true
from_address = "..."      # optional, defaults to smtp_username or email
```

No file, or a file with `email` left unset, means no notification -
entirely voluntary. A crash from a plain non-zero exit (a command
warning-and-skipping one bad recording, say) never notifies - only a
genuinely unhandled error does, and only when there's no tty attached
to see it directly. See `src/blackvue/core/notify.py`'s own module
docstring for the full design reasoning.

## SEE ALSO

`bv-download(1)`, the first command that reads a config this creates.
