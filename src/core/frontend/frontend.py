"""Frontend build directory utilities."""

import os


class Frontend:
    """
    Provides paths to the frontend build directory.

    Used by the SPA static file server to locate compiled frontend assets.

    Attributes:
        BUILD_DIR_NAME: Name of the build output directory.
        root_dir: Path to the project root.
        frontend_dir: Path to the frontend source directory.
        build_dir: Path to the compiled frontend assets.
    """

    BUILD_DIR_NAME = "dist"

    def __init__(self):
        """Initialize paths relative to this module's location."""
        self.root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
        self.frontend_dir = os.path.join(self.root_dir, "frontend")
        self.build_dir = os.path.join(self.frontend_dir, self.BUILD_DIR_NAME)

    def get_build_dir(self) -> str:
        """Returns the absolute path to the frontend build directory."""
        return self.build_dir

    def get_index_html(self) -> str:
        """Returns the absolute path to the index.html file."""
        return os.path.join(self.build_dir, "index.html")
