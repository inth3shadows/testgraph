"""Isolated so trace.py can put ONLY this directory on the target's PYTHONPATH.

harness/ holds `trace.py`, which SHADOWS the stdlib `trace` module for anything
the traced suite imports. Exporting the whole package into someone else's
interpreter to deliver one plugin is a side effect the measurement has no right
to have.
"""
