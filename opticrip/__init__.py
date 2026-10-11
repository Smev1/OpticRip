"""OpticRip - open-source optical disc ripper (DVD / Blu-ray -> MKV).

The package is split so the engine never depends on the GUI:

    opticrip.core       domain models, interfaces, config, errors
    opticrip.drives     optical drive discovery (OS specific)
    opticrip.disc       disc identification + DVD/Blu-ray structure parsers
    opticrip.rip        extraction, remuxing, validation, job queue
    opticrip.gui        PySide6 desktop front-end
    opticrip.cli        headless command line front-end
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
