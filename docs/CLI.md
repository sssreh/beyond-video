"The recording is the unit of work."



\# Command Line Interface



\## Philosophy



The command line interface is designed around two concepts:



1\. \*\*Recording selection\*\*

2\. \*\*Recording operation\*\*



Selecting recordings is independent of what is done with them.



Examples:



```text

bv-download Kirby --last-hours 2

bv-find Kirby --last-hours 2

bv-transcribe Kirby --last-hours 2

```



All recording-oriented commands support the same selection options.



\---



\# Camera system



The first argument is the camera system ID.



Example:



```text

bv-download Kirby

bv-find Kirby

bv-transcribe Kirby

```



The camera system ID identifies:



\- the camera

\- the configuration

\- the local archive



The ID is an ASCII string suitable for filenames and command lines.



The display name is independent and may contain UTF-8 characters and emojis.



\---



\# Recording selection



\## Recording type



```

\--type TYPE\[,TYPE...]

```



Supported values:



\- normal

\- event

\- manual

\- parking



Examples:



```text

\--type event



\--type event,manual



\--type normal,parking

```



\---



\## Relative time



```

\--last-minutes N



\--last-hours N



\--last-days N

```



Examples:



```text

bv-download Kirby --last-hours 2



bv-find Kirby --last-days 7

```



\---



\## Absolute time



```

\--from TIMESTAMP



\--until TIMESTAMP

```



Accepted timestamp formats:



```

YYYY



YYYYMM



YYYYMMDD



YYYYMMDD\_HH



YYYYMMDD\_HHMM



YYYYMMDD\_HHMMSS

```



Examples:



```text

\--from 2026



\--from 202607



\--from 20260715



\--from 20260715\_14



\--from 20260715\_143012

```



The timestamp precision determines the implied range.



Examples:



| Option | Value | Meaning |

|--------|-------|---------|

| from | 202607 | 2026-07-01 00:00:00 |

| until | 202607 | 2026-07-31 23:59:59 |

| from | 20260715 | 2026-07-15 00:00:00 |

| until | 20260715 | 2026-07-15 23:59:59 |

| from | 20260715\_14 | 2026-07-15 14:00:00 |

| until | 20260715\_14 | 2026-07-15 14:59:59 |



`--until` is inclusive.



\---



\## Filename matching



```

\--match PATTERN

```



Examples:



```text

\--match 20260714\*



\--match \*MF\*

```



\---



\## Latest recordings



```

\--latest N

```



Example:



```text

\--latest 10

```



Selects the latest N recordings.



Processing still occurs in chronological order.



\---



\# Combining selectors



Selectors may be combined.



Examples:



```text

bv-download Kirby \\

&#x20;   --type event,manual \\

&#x20;   --last-days 7



bv-transcribe Kirby \\

&#x20;   --type manual \\

&#x20;   --match 202607\*

```



All selectors are combined using logical AND.



\---



\# Ordering



Unless otherwise documented, recordings are processed in chronological order (oldest → newest).



For example:



```text

\--latest 3

```



selects the newest three recordings but processes them from oldest to newest.



This makes processing deterministic and simplifies recovery after interruptions.



