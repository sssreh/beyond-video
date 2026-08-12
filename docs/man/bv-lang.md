# bv-lang(1)

## NAME

`bv-lang` - manage argos-translate language packages used by `bv-generate --translate`

## SYNOPSIS

```
bv-lang list [--available]
bv-lang install SOURCE TARGET
```

## DESCRIPTION

`bv-lang` manages the offline translation language packages `bv-generate --translate` depends on. `--translate` translates a transcript from its spoken/detected language into a target language using argos-translate, which needs the matching source→target package installed locally first.

Installed packages are stored by argos-translate itself, not by Beyond Video - by default under `~/.local/share/argos-translate/packages` (Linux) or the equivalent per-OS user data directory. Set the `ARGOS_PACKAGES_DIR` environment variable (argos-translate's own, documented at <https://github.com/argosopentech/argos-translate/blob/master/docs/settings.md>) before running `bv-lang install`/`bv-generate --translate` to store packages somewhere else instead - e.g. to keep them alongside the rest of Beyond Video's data, or to persist them across container rebuilds (see `docker-compose.yml`, which sets this for both the `bv-web` and `bv-cli` services). This applies identically whether `bv-lang`/`bv-generate` run directly on the command line or inside a container - it's a plain environment variable, nothing Docker-specific about it.

## SUBCOMMANDS

### `bv-lang list [--available]`

List language packages.

| Option | Description |
|---|---|
| `--available` | List packages available to install instead of what's already installed locally. Needs network access. |

### `bv-lang install SOURCE TARGET`

Download and install a language package.

| Argument | Description |
|---|---|
| `SOURCE` | Source language code (e.g. `en`, `eng`). |
| `TARGET` | Target language code (e.g. `sv`, `swe`). |

## EXAMPLES

See what's already installed:

```
bv-lang list
```

See what could be installed (requires network):

```
bv-lang list --available
```

Install English→Swedish translation:

```
bv-lang install en sv
```

Then translate a day's recordings using it:

```
bv-generate --timestamp 20260715 --translate sv
```

## SEE ALSO

`bv-generate(1)`, specifically its `--translate LANG` flag.
