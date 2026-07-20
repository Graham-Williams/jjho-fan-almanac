"""Flask web app for The Fan Almanac.

Factory: :func:`create_app`. Serve with gunicorn:
``gunicorn -w 2 --threads 8 'jjho.web:create_app()'``.

This is the SKELETON: it boots with a health route and a placeholder home
page behind the shared-password gate. The four features (Super Search, the
Book of Settled Law, Motifs & Running Bits, Justice Statistics) are built on
feature branches — see ``DESIGN.md`` for the phased build order.
"""

from .app import create_app

__all__ = ["create_app"]
