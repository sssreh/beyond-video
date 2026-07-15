\# Testing



\## Philosophy



Tests verify one feature at a time.



Each test should:



\- have one purpose

\- be repeatable

\- be idempotent

\- be independent of other tests

\- produce deterministic output



Tests should never require manual cleanup.



\---



\# Test categories



The project contains two categories of tests.



\## Camera tests



Camera tests require an online camera.



These tests verify:



\- connectivity

\- recording listing

\- downloading

\- live status

\- monitoring



Camera tests should minimise camera online time.



\---



\## Firmware tests



Firmware tests operate entirely on firmware images.



These tests verify:



\- archive layout

\- configuration files

\- version information

\- resources

\- CGI scripts



Firmware tests may be executed offline.



\---



\# Test output



Every test begins with:



TEST:

Purpose:

Expected:



Example:



TEST: test\_list



Purpose:

&#x20;   Verify recording listing.



Expected:

&#x20;   Recording list returned.



\---



Every test finishes with exactly one result:



PASS



FAIL



SKIPPED



No other summary should be produced.



\---



\# Test naming



Tests are named after the feature they verify.



Examples:



test\_connect.py



test\_list.py



test\_download.py



test\_resume.py



test\_monitor.py



\---



\# Test independence



Tests must not depend on:



\- execution order

\- previous tests

\- manual cleanup



Running the same test multiple times shall produce the same result.



\---



\# Logging



Tests should log only information useful for diagnosis.



Normal successful execution should remain concise.



\---



\# Future



When stable, manual tests may become automated regression tests.



