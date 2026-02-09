#!/usr/bin/env python3
"""
Upload local database to Railway.

This script outputs the database as base64, which can be piped to Railway.

Usage:
    # On local machine:
    python scripts/upload_db_to_railway.py --encode > data/db_backup.b64

    # Then use railway run to restore:
    railway run python scripts/upload_db_to_railway.py --decode < data/db_backup.b64
"""

import argparse
import base64
import sys
import os


def encode_db(db_path: str):
    """Encode database to base64 and print to stdout."""
    with open(db_path, 'rb') as f:
        data = f.read()

    encoded = base64.b64encode(data).decode('ascii')
    print(encoded)
    print(f"Encoded {len(data)} bytes -> {len(encoded)} chars", file=sys.stderr)


def decode_db(db_path: str):
    """Read base64 from stdin and write to database file."""
    # Backup existing db if present
    if os.path.exists(db_path):
        backup_path = db_path + '.backup'
        os.rename(db_path, backup_path)
        print(f"Backed up existing db to {backup_path}", file=sys.stderr)

    # Read base64 from stdin
    encoded = sys.stdin.read().strip()
    data = base64.b64decode(encoded)

    with open(db_path, 'wb') as f:
        f.write(data)

    print(f"Decoded {len(encoded)} chars -> {len(data)} bytes", file=sys.stderr)
    print(f"Database written to {db_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='Upload database to Railway')
    parser.add_argument('--encode', action='store_true', help='Encode local DB to base64')
    parser.add_argument('--decode', action='store_true', help='Decode base64 from stdin to DB')
    parser.add_argument('--db', default=None, help='Database path')

    args = parser.parse_args()

    # Determine db path
    if args.db:
        db_path = args.db
    elif os.environ.get('DATABASE_PATH'):
        db_path = os.environ['DATABASE_PATH']
    else:
        db_path = 'handelsregister.db'

    if args.encode:
        encode_db(db_path)
    elif args.decode:
        decode_db(db_path)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
