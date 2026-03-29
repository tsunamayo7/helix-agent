"""PathGuard: file access security for the agent loop."""

from __future__ import annotations

from pathlib import Path

# Default allowed directories
DEFAULT_ALLOWED_ROOTS = [
    Path("C:/Development"),
    Path("C:/Users/tomot/Documents"),
]

# Blocked file patterns (case-insensitive)
BLOCKED_PATTERNS = [
    ".env", ".secret", "credentials", ".ssh", ".gnupg", ".gpg",
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
    ".npmrc", ".pypirc", "token", "password", "api_key",
    "access.json",  # Discord access config
]

# Blocked extensions
BLOCKED_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}


class PathGuard:
    """Validates file paths against an allowlist and blocks sensitive files."""

    def __init__(self, allowed_roots: list[Path] | None = None):
        self.allowed_roots = [r.resolve() for r in (allowed_roots or DEFAULT_ALLOWED_ROOTS)]

    def validate(self, path: str) -> Path:
        """Validate and resolve a path. Raises PermissionError if blocked."""
        resolved = Path(path).resolve()

        # Check allowlist
        if not any(self._is_under(resolved, root) for root in self.allowed_roots):
            raise PermissionError(
                f"Access denied: {path} is outside allowed directories "
                f"({', '.join(str(r) for r in self.allowed_roots)})"
            )

        # Check blocked patterns
        name_lower = resolved.name.lower()
        for pattern in BLOCKED_PATTERNS:
            if pattern in name_lower:
                raise PermissionError(f"Sensitive file blocked: {resolved.name}")

        # Check blocked extensions
        if resolved.suffix.lower() in BLOCKED_EXTENSIONS:
            raise PermissionError(f"Sensitive file type blocked: {resolved.suffix}")

        return resolved

    def _is_under(self, path: Path, root: Path) -> bool:
        """Check if path is under root (prevents traversal attacks)."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
