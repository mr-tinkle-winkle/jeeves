"""Jeeves -- a local-first voice assistant for Linux (NixOS).

The package is split the way SPEC.md asks:

* ``jeeves.daemon``    -- does all of the work (audio, models, functions).
* ``jeeves.gui``       -- the settings front-end; only sends requests to the daemon.
* ``jeeves.overlay``   -- on-screen indicators and popups, started by the daemon
                          so they work without the GUI open.
* ``jeeves.functions`` -- the Dictionary: full and partial functions.
"""

__version__ = "0.1.0"
