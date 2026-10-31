"""OpticRip - clean-room optical disc ripper.

The product is split into a headless engine (``opticrip.cli``) and a Tk user
interface (``opticrip.gui``) exactly as the reference architecture requires
(CLONE_SPEC.md R-ARCH-01).  The engine owns every disc operation; the UI only
starts the engine and renders its events.
"""

__version__ = "0.9.0"
PRODUCT = "OpticRip"
PRODUCT_TAG = "OpticRip BETA"
# Format of the banner line (R-IDN-01).  The reference prints its own product
# name here; the *shape* is what matters for consumers that parse the log.
BANNER = f"{PRODUCT} v{__version__} win(x64-release)"
GUI_VERSION = f"v{__version__}"
COPYRIGHT = "Copyright (C) 2026 OpticRip contributors"
