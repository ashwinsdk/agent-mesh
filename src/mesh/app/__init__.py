"""The `mesh` interactive app (Textual). Imported lazily: hooks, workers and the conductor never import this package."""


def run(paths) -> int:
    try:
        import textual  # noqa: F401
    except ImportError:
        raise SystemExit("The mesh app needs the 'textual' package. Install it with:\n"
                         "  pipx install --force git+https://github.com/ashwinsdk/agent-mesh.git\n"
                         "or use `mesh status` for a plain-text view.")
    from .ui import MeshApp
    MeshApp(paths).run()
    return 0
