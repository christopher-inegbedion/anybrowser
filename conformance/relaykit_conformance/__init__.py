"""The engine contract, as an executable test suite.

Run it against any registered engine::

    pytest --pyargs relaykit_conformance --engine chrome
    pytest --pyargs relaykit_conformance --engine my-firefox -o k=v

Every test is gated on the capabilities the engine declares, so a backend that
honestly reports it cannot drag is not failed for not dragging -- it is failed
for *claiming* it can and then not doing it. That asymmetry is the point: the
suite tests truthfulness as much as function.

A green run is what the README means by "the agent runs on your browser".
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
