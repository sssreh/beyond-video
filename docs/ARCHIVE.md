\# Archive Specification



\## Purpose



An archive is a long-term, self-contained collection of recordings and their

associated metadata.



The archive is designed to:



\- preserve recordings and metadata without loss of information;

\- preserve the camera configuration history;

\- allow recordings to be interpreted without access to the original camera;

\- remain independent of implementation details and programming language.



This document specifies the archive format. It does not describe how the

archive is implemented.





\## Terminology



\### Recording



A recording is identified by a `RecordingId` and may contain one or more

assets.



Examples of assets include:



\- video

\- GPS data

\- thumbnail

\- event data



\### Asset



A file belonging to a recording.



Examples:



```

20260715\_145132\_N.mp4

20260715\_145132\_N.gps

20260715\_145132\_N.thm

```



\### Configuration Snapshot



A configuration snapshot is a complete `config.ini` associated with a specific

`RecordingId`.



Configuration snapshots describe the camera configuration that was active from

that recording onward.





\## Directory Structure



An archive consists of files identified by `RecordingId`.



Example:



```

20260715\_145132\_N.mp4

20260715\_145132\_N.gps

20260715\_145132\_N.thm

20260715\_145132\_N.config.ini



20260715\_205422\_P.mp4

20260715\_205422\_P.gps

```



Files belonging to the same recording share the same `RecordingId`.





\## RecordingId



A `RecordingId` uniquely identifies a recording.



A `RecordingId` consists of:



\- date

\- time

\- recording mode



Example:



```

20260715\_145132\_N

```



The recording mode is part of the identifier and therefore distinguishes

recordings that would otherwise have identical timestamps.



`RecordingId` defines the chronological ordering of recordings within an

archive.





\## Assets



A recording may contain one or more assets.



Supported asset types are implementation dependent.



Missing assets do not invalidate a recording.





\## Configuration Snapshots



Configuration snapshots are stored as complete `config.ini` files.



Example:



```

20260715\_145132\_N.config.ini

```



Only complete configuration snapshots are stored.



Configuration differences are never stored.



\### Archive Initialization



The first recording in every archive shall have a corresponding

`RecordingId.config.ini`.



This establishes the initial configuration of the archive.



\### Configuration Changes



Whenever the camera configuration changes, a new complete configuration

snapshot shall be created using the `RecordingId` of the first recording made

with the new configuration.



\### Configuration Lookup



The active configuration for a recording is determined by selecting the latest

configuration snapshot whose `RecordingId` is less than or equal to the

recording's `RecordingId`.



In other words:



```

configuration(recording) =

&#x20;   latest config.ini where

&#x20;   config.RecordingId <= recording.RecordingId

```



This guarantees that every recording has exactly one active configuration.





\## Camera Population



An archive is populated by at most one camera at any point in time.



A camera may be replaced during the lifetime of an archive.



When this occurs, the downloader shall create a new configuration snapshot

containing the identity and configuration of the replacement camera.



Simultaneous population of an archive by multiple cameras is not supported.



Merging independently created archives is not supported.





\## Archive Invariants



A valid archive satisfies the following rules.



\- RecordingIds are unique.

\- Recordings are chronologically ordered by `RecordingId`.

\- The first recording has a corresponding configuration snapshot.

\- Configuration snapshots are complete `config.ini` files.

\- Configuration snapshots are created only when the camera configuration

&#x20; changes.

\- Every recording has exactly one active configuration.

\- At most one camera populates the archive at any point in time.





\## Error Recovery



If a valid configuration cannot be determined for a recording, software may use

camera-specific fallback values.



For example, grouping algorithms may use the maximum expected recording gap for

the camera model.



Software should emit a warning whenever fallback values are used.





\## Future Extensions



The archive format is intended to evolve while remaining backward compatible.



Possible future extensions include:



\- additional asset types;

\- additional camera brands;

\- camera replacement history;

\- archive validation tools;

\- archive repair tools.

