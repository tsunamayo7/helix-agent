import pathlib

def validate_path(filepath: str, base_dir: str) -> bool:
    """Checks if filepath is safely within base_dir (no path traversal).

    Args:
        filepath (str): The path to the file to validate.
        base_dir (str): The base directory that the file should be within.

    Returns:
        bool: True if the path is safely within base_dir, False otherwise.
    """
    try:
        base = pathlib.Path(base_dir).resolve()
        target = pathlib.Path(filepath).resolve()
        return base in target.parents or target == base
    except (OSError, RuntimeError):
        return False
