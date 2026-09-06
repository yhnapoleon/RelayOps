"""Lightweight source-only disclosure guard. Does not replace human review."""
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', 'node_modules', 'dist', '__pycache__', '.venv', 'venv',
            '.pytest_cache', '.cache', 'data', 'logs', 'outputs', 'tmp'}
BLOCKED_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.7z', '.zip', '.pem', '.key'}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16})\b'),
    'personal Windows path': re.compile(r'(?i)[a-z]:[\\/]Users[\\/][^\\/\s]+[\\/]'),
    'private IPv4 address': re.compile(r'\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
}


def candidates():
    if (ROOT / '.git').exists():
        names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z'], text=True).split('\0')
        return [ROOT / name for name in names if name]
    files = []
    for directory, children, names in os.walk(ROOT):
        children[:] = [n for n in children if n not in EXCLUDED]
        files.extend(Path(directory) / n for n in names)
    return files


def main():
    errors = []
    files = candidates()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.name in {'.env', 'config.yaml'} or path.suffix.lower() in BLOCKED_SUFFIXES:
            errors.append(f'{relative}: local configuration or binary data must not be tracked')
        if path == Path(__file__).resolve() or path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.woff2'}:
            continue
        try:
            value = path.read_text('utf-8')
        except (UnicodeError, OSError):
            errors.append(f'{relative}: unexpected non-text file; review explicitly')
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(value):
                line = value.count('\n', 0, match.start()) + 1
                errors.append(f'{relative}:{line}: {label}')
    for error in errors:
        print(error)
    print(f'Checked {len(files)} candidate files; {len(errors)} findings.')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
