Vocabulary is part of the architecture.



If two developers use different words for the same thing, they will eventually write different software.



\# Glossary



This document defines the terminology used throughout the project.



The same concept should always use the same name.



If a new term is needed, it should be added here before it appears in code or documentation.



\---



\## System



The complete installation.



A System usually consists of:



\- one vehicle

\- one or more dashcam systems

\- storage configuration

\- synchronization policies

\- archive policies



Example:



&#x20;   Kirby



\---



\## Vehicle



The physical vehicle.



Examples:



\- Lexus GS450h

\- Volvo XC60



A vehicle belongs to one System.



\---



\## Dashcam System



A single recording device.



A Dashcam System has:



\- one firmware

\- one configuration

\- one synchronization state

\- one or more communication endpoints

\- one or more channels



Examples:



\- BlackVue DR900S-2CH

\- BlackVue DR970X-2CH



\---



\## Channel



A video channel produced by a Dashcam System.



Examples:



\- Front

\- Rear

\- Interior



A channel is not an independent Dashcam System.



\---



\## Endpoint



A communication path to a Dashcam System.



Examples:



\- Home WiFi

\- Router WiFi

\- Static IP



An Endpoint consists of:



\- host

\- port

\- protocol

\- priority



\---



\## Recording



The atomic unit of synchronization.



A Recording represents one recording event and contains one or more Assets.



Operations such as download, verification, moving and deletion always operate on a complete Recording.



\---



\## Asset



A file belonging to a Recording.



Examples:



\- Front video

\- Rear video

\- Front thumbnail

\- Rear thumbnail

\- GPS

\- G-sensor



Assets should never be treated as independent recordings.



\---



\## Adapter



Vendor-specific implementation.



The Adapter translates between the project's common domain model and the manufacturer's implementation.



Examples:



\- BlackVue Adapter

\- VIOFO Adapter

\- Thinkware Adapter



\---



\## Firmware



The software version running on a Dashcam System.



Firmware determines which Adapter implementation should be used.



\---



\## Storage



The destination where Recordings are preserved.



Storage layout is independent of synchronization.



Examples:



\- Flat

\- Year

\- Year/Month



\---



\## Synchronization



The process of discovering, downloading and verifying new Recordings.



Synchronization never modifies the original Recording.



\---



\## Archive



Long-term preservation of Recordings.



Archive management includes:



\- storage organization

\- retention

\- verification

\- reporting



Archive management is separate from Synchronization.



\---



\# Preferred Terminology



To keep the codebase and documentation consistent, use the preferred terms below.



\- \*\* Avoid:\*\* Camera  

&#x20; \*\*Prefer:\*\* Dashcam System or Channel  

&#x20; \*\*Reason:\*\* "Camera" can refer either to the recording unit or to an image sensor.



\- \*\* Avoid:\*\* File  

&#x20; \*\*Prefer:\*\* Asset  

&#x20; \*\*Reason:\*\* A Recording consists of one or more Assets.



\- \*\* Avoid:\*\* Record  

&#x20; \*\*Prefer:\*\* Recording  

&#x20; \*\*Reason:\*\* Use the same term consistently throughout the project.



\- \*\* Avoid:\*\* IP  

&#x20; \*\*Prefer:\*\* Endpoint  

&#x20; \*\*Reason:\*\* A Dashcam System may have multiple communication Endpoints.



\- \*\* Avoid:\*\* Device  

&#x20; \*\*Prefer:\*\* Dashcam System  

&#x20; \*\*Reason:\*\* "Device" is too generic within this project.



