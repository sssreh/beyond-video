\# Architecture



\## Purpose



The goal of this project is to create an open, privacy-focused, cross-platform framework for understanding, documenting and automating dashcams.



Although the first supported manufacturer is BlackVue, the architecture is designed to support additional manufacturers without requiring changes to the core framework.



The framework should run on Linux, Windows, macOS, Raspberry Pi, Synology NAS and other systems capable of running Python.



\---



\# Design Principles



The following principles guide all development.



\## Vendor-neutral core



The core framework must never depend on a specific manufacturer.



Manufacturer-specific functionality belongs in adapters.



\## Vehicle-centric



The framework manages vehicles rather than cameras.



A vehicle may contain one or more cameras and one or more network endpoints.



\## Multiple connection endpoints



Each vehicle may have multiple connection endpoints.



Examples include:



\* Home Wi-Fi

\* Camera Wi-Fi

\* Mobile router

\* Static public IP

\* VPN



Endpoints have priorities and are automatically selected by the Connection Manager.



\## Automatic discovery



Never ask the user for information that the software can discover automatically.



Examples include:



\* Camera model

\* Firmware version

\* Supported capabilities

\* Recording directory

\* File naming conventions



\## Preserve camera compatibility



Downloaded files should remain compatible with the manufacturer's own software whenever possible.



For BlackVue this means keeping recordings, thumbnails, GPS files and G-sensor files together in the same directory.



The framework should never reorganize recordings in a way that breaks compatibility with BlackVue Viewer.



\## Cross-platform



The project must work on:



\* Linux

\* Windows

\* macOS

\* Raspberry Pi

\* Synology NAS



Platform-specific code should be avoided whenever possible.



\## Community driven



The project welcomes:



\* Developers

\* Reverse engineers

\* Hardware testers

\* Documentation contributors



\---



\# High-level Architecture



```

Fleet

&#x20;   │

&#x20;   ▼

Vehicle

&#x20;   │

&#x20;   ▼

Connection Manager

&#x20;   │

&#x20;   ▼

Endpoint

&#x20;   │

&#x20;   ▼

Adapter

&#x20;   │

&#x20;   ▼

Camera

&#x20;   │

&#x20;   ▼

Jobs

&#x20;   │

&#x20;   ▼

Storage

```



\---



\# Fleet



A fleet is the collection of all configured vehicles.



Examples:



\* Personal vehicles

\* Company fleet

\* Family vehicles



\---



\# Vehicle



A vehicle represents a physical vehicle.



Example:



Kirby



A vehicle contains:



\* One or more cameras

\* One or more connection endpoints

\* Storage configuration



\---



\# Camera



A camera contains information discovered automatically.



Examples:



\* Manufacturer

\* Model

\* Firmware version

\* Capabilities



The user should rarely need to configure these values manually.



\---



\# Connection Manager



The Connection Manager selects the best available endpoint.



Selection is based on:



\* Priority

\* Availability

\* User preferences

\* Operation requirements



The Connection Manager hides network details from adapters.



\---



\# Endpoint



Examples include:



\* Home Wi-Fi

\* Camera Wi-Fi

\* Static public IP

\* VPN



Each endpoint has:



\* Name

\* Address

\* Priority

\* Timeout

\* Optional policy



\---



\# Adapter



Adapters translate manufacturer-specific protocols into the common framework interface.



The first adapter is:



BlackVue



Future adapters may include:



\* VIOFO

\* Thinkware

\* Garmin

\* 70mai



\---



\# Firmware Profiles



Firmware is often more important than the camera model.



Firmware profiles describe:



\* Supported capabilities

\* Known limitations

\* Protocol differences

\* Authentication requirements



Adapters use firmware profiles to determine behaviour.



\---



\# Jobs



Jobs perform work using the framework.



Examples:



\* Probe

\* Synchronize

\* Verify

\* Backup

\* Firmware analysis

\* Health check



Jobs should not contain manufacturer-specific logic.



\---



\# Storage



The framework stores recordings using a predictable layout while preserving manufacturer compatibility.



Example:



```

Dashcams/

&#x20;   Kirby/

&#x20;       Record/

&#x20;           \*.mp4

&#x20;           \*.gps

&#x20;           \*.3gf

&#x20;           \*.thm



&#x20;       .dashcam/

&#x20;           state.db

&#x20;           logs/

&#x20;           cache/

```



Application metadata should never be mixed with camera recordings.



\---



\# Configuration



The user should configure only information that cannot be discovered automatically.



Typical configuration:



\* Vehicle name

\* Storage root

\* Connection endpoints



Everything else should be supplied by the adapter whenever possible.



\---



\# Long-term Vision



The project aims to become a vendor-neutral dashcam automation framework.



BlackVue is the first supported manufacturer.



Future manufacturers should be supported by implementing new adapters rather than modifying the core framework.



The architecture should remain stable as additional manufacturers, firmware versions and features are added.



