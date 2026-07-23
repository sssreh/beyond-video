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
