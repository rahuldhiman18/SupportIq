"""Service modules.

Modules are intentionally imported by their consumers instead of eagerly
imported here. This keeps the package lightweight and lets
``python -m app.services.worker`` execute the worker module without a
runpy double-import warning.
"""

