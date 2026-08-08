\# The Story Behind Beyond Video



> \*Every software project has a history. Most histories are written as changelogs – lists of features, bug fixes and version numbers. This is not that kind of history. This is the story of how Beyond Video came to be.\*



\---



\## It started with a dashcam



Like many drivers, I had accumulated years of BlackVue dashcam recordings.



Thousands of video files were neatly organized on disk, yet surprisingly difficult to explore. They were recordings, not journeys. Finding a particular drive meant navigating directories, remembering dates, and manually piecing together front camera, rear camera, GPS tracks, and metadata.



The recordings already contained everything.



The problem was that they had never been treated as one coherent archive.



Beyond Video began with a simple question:



> \*\*What if a dashcam archive behaved like an archive instead of a collection of files?\*\*



That single question shaped everything that followed.



\---



\## The archive came first



It was tempting to begin with video processing.



Instead, the first effort went into understanding the archive itself.



Recordings became objects instead of filenames.



Recording IDs became stable identities instead of strings.



Assets such as videos, GPS logs, transcripts and generated files became related pieces belonging to the same recording.



The archive itself became the center of the design.



Almost every feature that exists today exists because this foundation was built first.



\---



\## Simplicity became a design rule



Throughout development there was one recurring question:



> \*\*Can this be made simpler?\*\*



Whenever a feature required additional rules, another approach was sought.



The goal was never to create the cleverest software.



The goal was to create software that would still make sense many years later.



That philosophy influenced almost every part of the project.



\---



\## The dream



One night, the solution appeared unexpectedly.



Not in front of a keyboard.



Not during debugging.



But in a dream.



The problem was searching for partial timestamps.



Traditional date parsing quickly becomes complicated.



Years have twelve months.



Months have different numbers of days.



Leap years exist.



Users rarely type complete timestamps.



Many systems solve this by introducing increasingly complicated parsing rules.



The dream suggested something radically simpler.



Treat timestamps as \*\*lexical\*\*, not calendar-aware.



Instead of asking:



> \*\*"Is this a valid date?"\*\*



the software only asks:



> \*\*"How does this string compare?"\*\*



A partial timestamp simply becomes a lexical interval.



Missing digits are filled with \*\*0\*\* for the lower bound and \*\*9\*\* for the upper bound.



For example:



| User input | Lower bound | Upper bound |

|------------|-------------|-------------|

| `2025`    | `20250000\_000000` | `20259999\_999999` |

| `202506` | `20250600\_000000` | `20250699\_999999` |

| `20250630\_154` | `20250630\_154000` | `20250630\_154999` |



Nothing more.



No leap years.



No month lengths.



No calendar calculations.



Human dates became a presentation problem.



Searching became a string comparison problem.



That single idea spread throughout the entire project.



\- Filenames

\- Recording IDs

\- Sorting

\- Searching

\- Filtering

\- Command-line arguments

\- Archive traversal



Everywhere the same representation could be reused without translation.



Only at the very edge of the system does human formatting appear.



The archive itself remains completely lexical.



Looking back, this was probably the single most important architectural decision in the project.



\---



\## Building the tools



Once the archive model was stable, the tools almost built themselves.



`bv-ls` made the archive searchable.



`bv-gps` understood locations.



`bv-generate` produced derived assets.



`bv-export` transformed recordings into complete journeys with stitched videos, GPS tracks, subtitles, maps, transcripts and g-sensor overlays.



Later, a web interface appeared.



Importantly, it was never a second implementation.



The browser simply became another client of exactly the same archive library.



The architecture never changed.



\---



\## Beyond recordings



Somewhere during development the purpose of the project quietly changed.



Originally it managed recordings.



Eventually it started telling journeys.



A drive could become a stitched video.



The route appeared on a moving map.



GPS data and g-sensor information became visual overlays.



Speech became subtitles.



Raw recordings became stories people could watch and understand.



The software had gone \*\*beyond video\*\*.



The name had become true.



\---



\## Small details matter



Not every feature exists because it is necessary.



Some exist simply because software should also be enjoyable.



The home page contains a hidden submarine.



One day it may quietly move across Copenhagen Harbour while nobody is looking.



Most users will never notice.



A few will.



Those little discoveries are part of the personality of Beyond Video.



\---



\## No database



One design decision surprises many people.



Beyond Video avoids introducing a database whenever the filesystem already provides the answer.



Stable filenames.



Stable recording identifiers.



Lexical timestamps.



Simple directory structures.



The filesystem itself becomes the index.



That decision has kept the project portable, understandable and remarkably simple.



\---



\## The web interface



The web interface was never intended to replace the command line.



Instead, it provides another way of using exactly the same architecture.



The command line exposes every capability directly.



The browser guides the user.



Features are grouped naturally into what is required, what is commonly used, and what is advanced.



The goal is not to remove power.



It is to hide unnecessary complexity until it is needed.



Even the interface reflects the philosophy of the project.



The home page is vibrant and inviting.



Once work begins, the background fades into the distance so the user's attention remains on the task rather than the decoration.



\---



\## A promise kept



For a long time the submarine was a single sentence in this file.



> \*One day it may quietly move across Copenhagen Harbour while nobody is looking.\*



It stayed exactly that: a sentence, and a handful of pixels baked into a JPEG.



Then the promise was tested.



Christer opened the welcome page and looked for it, and could not find it.



Not because it had vanished. The shape was still sitting in the water, exactly where it had always been.



But hidden and invisible turned out to be very different things.



A shadow can blend so far into water that even the person who asked for it can no longer see it.



That accident became the real lesson.



A hidden detail only works if someone can eventually discover it.



Contrast was tuned twice more, each pass checked at the size a person actually looks at a webpage, not a zoomed-in crop.



Then the sentence itself was rewritten, one request at a time.



First: does five minutes feel too long for something almost nobody will see.



Then: move it for real. A small overlay, over the water, to the left.



Then, mid-thought, a correction. Slower. North to south, the way a submarine on patrol behaves, not a diagonal cut across the harbor.



Surface.



Drift for twenty or thirty seconds.



Dive, leaving a ring behind it.



Disappear for a minute or two.



Surface somewhere else.



And a better idea on top of that one.



Do not run it continuously.



Let every visit to the page roll its own one-in-five chance the submarine shows up at all.



Most visits, nothing.



Occasionally, a small shape drifting through open water that nobody asked to see and almost nobody will notice.



The old static shape \- the one that needed three rounds of contrast tuning just to be seen at all \- was removed from the photograph entirely.



In its place, a small script now redraws the harbor's own geometry every frame, so whatever moves through it stays exactly where the water actually is, at any window size, on any screen.



The sentence in this file no longer describes something that might happen someday.



It describes something the page now quietly does.



\---



\## Looking forward



Beyond Video will probably never be finished.



There will always be another camera model.



Another visualization.



Another export option.



Another bug to fix.



But the architecture has reached the point every software project hopes for.



New ideas fit naturally.



Old ideas rarely need to change.



That is usually a sign that the foundation is doing its job.



\---



\## Acknowledgements



Although Beyond Video was written by one developer, it was not developed entirely alone.



Large language models became collaborators during its development.



One became an architectural sounding board, constantly asking whether an idea could be made simpler.



Another became an extraordinarily productive implementation partner, turning designs into working code at remarkable speed.



Neither designed the project.



Neither wrote it alone.



But both influenced it in different ways.



Ultimately, Beyond Video reflects one guiding principle that remained unchanged from the first day until today:



> \*\*Make the software simpler than the problem.\*\*



\---



> \*"Every recording is just a collection of files until someone tells its story."\*



\*\*Beyond Video\*\* exists to tell that story.



