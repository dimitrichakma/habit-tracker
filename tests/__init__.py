"""Non-mocked integration tests (Phase 6.3).

Makes ``tests`` a package so pytest puts the project root on ``sys.path`` and
``import src.*`` resolves — the same reason ``evaluation/__init__.py`` exists.
``src/`` never imports from here.
"""
