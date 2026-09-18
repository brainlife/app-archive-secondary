"""Verified secondary transfers, adapted from app-archive fcf0374.

Secondary archives merge requests: they never delete unrelated destination keys.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath


MAX_SINGLE_COPY = 5 * 1024**3
BATCH_SIZE = 10000


def s3_parts(uri):
    bucket, separator, key = uri.removeprefix('s3://').partition('/')
    if not uri.startswith('s3://') or not bucket or not separator or not key:
        raise ValueError('Expected an S3 URI with a bucket and nonempty key/prefix')
    return bucket, key


def resolve_source(source):
    """Map known mounted S3 sources to their objects; local outputs stay local.

    The mount bucket must be explicit: a destination bucket does not establish
    which bucket a filesystem mount represents.
    """
    if source.startswith('s3://'):
        s3_parts(source)
        return source
    bucket = os.environ.get('BRAINLIFE_ARCHIVE_Secondary_BUCKET')
    if not bucket:
        return source
    path = Path(source).resolve()
    mounts = {os.environ.get('BRAINLIFE_ARCHIVE_s3fs', '/mnt/s3fs'),
              os.environ.get('BRAINLIFE_ARCHIVE_embargo', '/mnt/s3fs')}
    for mount in mounts:
        try:
            relative = path.relative_to(Path(mount).resolve())
        except ValueError:
            continue
        if str(relative) == '.':
            raise ValueError('Refusing to archive an entire S3 mount')
        return 's3://' + bucket + '/' + relative.as_posix()
    return source


def s3_client():
    import boto3
    return boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL'),
                        region_name=os.environ.get('AWS_REGION'))


def objects(client, bucket, prefix):
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get('Contents', [])


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value:
        raise ValueError('Archive file paths must be relative and cannot contain .. or backslashes')
    return '' if str(path) == '.' else str(path)


def local_entries(path, directory):
    if not path.exists():
        raise FileNotFoundError(path)
    if not directory:
        if not path.is_file():
            raise ValueError('Expected a regular file: ' + str(path))
        yield str(path), '', path.stat().st_size
        return
    if not path.is_dir():
        raise ValueError('Expected a directory: ' + str(path))
    # Follow staged links, but reject cycles rather than looping indefinitely.
    def walk(folder, relative, ancestors):
        stat = folder.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in ancestors:
            raise ValueError('Directory symlink cycle: ' + str(folder))
        empty = True
        with os.scandir(folder) as entries:
            for entry in entries:
                empty = False
                rel = relative + entry.name
                if entry.is_dir():
                    yield from walk(Path(entry.path), rel + '/', ancestors | {identity})
                elif entry.is_file():
                    yield entry.path, rel, entry.stat().st_size
                else:
                    raise ValueError('Not a regular file: ' + entry.path)
        if empty:
            yield None, relative, 0  # Preserve empty directories as S3 markers.
    yield from walk(path, '', set())


def remote_entries(client, uri, directory):
    bucket, key = s3_parts(uri)
    if not directory:
        from botocore.exceptions import ClientError
        try:
            response = client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                raise FileNotFoundError(uri) from error
            raise
        yield uri, '', response['ContentLength']
        return
    prefix = key.rstrip('/') + '/'
    found = False
    for obj in objects(client, bucket, prefix):
        found = True
        yield 's3://' + bucket + '/' + obj['Key'], obj['Key'][len(prefix):], obj['Size']
    if not found:
        raise FileNotFoundError(uri)


def command_argument(value):
    # s5cmd run uses shell-style tokenization, but does not execute a shell.
    # Reject line breaks, which would create additional commands in the run file.
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Archive paths cannot contain line breaks or NUL')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def archive_directory(source_root, bucket, prefix):
    """Return archived size after verifying every copied key (merge semantics)."""
    safe_relative(prefix)
    if not bucket or '/' in bucket or not prefix.endswith('/') or len(prefix.split('/')) < 6:
        raise ValueError('A request-scoped secondary destination is required')
    source_root = resolve_source(source_root).rstrip('/')
    print('Inspecting Secondary archive source: ' + source_root, flush=True)
    remote = source_root.startswith('s3://')
    client = s3_client() if remote else None
    with tempfile.TemporaryDirectory(prefix='archive-secondary-') as temporary:
        # Keep millions of paths on disk rather than in Python memory.
        with sqlite3.connect(os.path.join(temporary, 'manifest.sqlite')) as manifest:
            manifest.execute('CREATE TABLE files (key TEXT PRIMARY KEY, source TEXT, size INTEGER, seen INTEGER DEFAULT 0)')
            for selected, name, directory, required in [('', '', True, True)]:
                source = source_root + ('/' + selected if selected else '')
                entries = (remote_entries(client, source, directory) if remote else
                           local_entries(Path(source).absolute(), directory))
                started = False
                try:
                    for src, relative, size in entries:
                        started = True
                        key = '/'.join(p for p in (name, relative) if p)
                        if (src is None or (remote and src.endswith('/'))) and key and not key.endswith('/'):
                            key += '/'
                        safe_relative(key)
                        if src is not None:
                            command_argument(src)
                        command_argument(key)
                        previous = manifest.execute('SELECT source, size FROM files WHERE key=?', (key,)).fetchone()
                        if previous and previous != (src, size):
                            raise ValueError('Conflicting archive file definitions: ' + key)
                        manifest.execute('INSERT OR IGNORE INTO files(key, source, size) VALUES (?, ?, ?)', (key, src, size))
                except FileNotFoundError:
                    if required or started:
                        raise
                    print('Optional archive source missing: ' + source, flush=True)
            count, total = manifest.execute('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files').fetchone()
            if not count:
                raise ValueError('Refusing to archive an empty Secondary dataset')
            executable = shutil.which('s5cmd')
            if not executable:
                raise RuntimeError('Secondary archiving requires s5cmd in the archive container')
            destination = f's3://{bucket}/{prefix}'
            command_argument(destination)
            if remote:
                src_bucket, src_key = s3_parts(source_root)
                if bucket == src_bucket and (prefix.startswith(src_key.rstrip('/') + '/') or
                                             (src_key.rstrip('/') + '/').startswith(prefix)):
                    raise ValueError('Source and archive prefixes must not overlap')
            client = client or s3_client()
            workers = int(os.environ.get('BRAINLIFE_ARCHIVE_S5CMD_WORKERS', '64' if remote else '8'))
            if workers < 1:
                raise ValueError('BRAINLIFE_ARCHIVE_S5CMD_WORKERS must be positive')
            print(f'Secondary s5cmd archive: {count} objects, {total} bytes -> {destination}', flush=True)
            cursor = manifest.execute('SELECT key, source, size FROM files ORDER BY key')
            completed = 0
            while True:
                batch = cursor.fetchmany(BATCH_SIZE)
                if not batch:
                    break
                commands = os.path.join(temporary, 'commands.txt')
                sdk_copies = []
                command_count = 0
                with open(commands, 'w') as output:
                    for key, src, size in batch:
                        if src is None:
                            client.put_object(Bucket=bucket, Key=prefix + key, Body=b'')
                            continue
                        if remote and (' ' in src or src.endswith('/') or size > MAX_SINGLE_COPY):
                            # v2.3.0 QueryEscape encodes spaces as + in CopySource;
                            # some S3 endpoints interpret those as literal pluses.
                            # The SDK encodes CopySource correctly for these keys.
                            sdk_copies.append((key, src, size))
                            continue
                        output.write('cp --raw --concurrency 2 --part-size 16 ' + command_argument(src) + ' ' + command_argument(destination + key) + '\n')
                        command_count += 1
                if command_count:
                    subprocess.run([executable, '--numworkers', str(workers), '--log', 'error', 'run', commands], check=True)
                if sdk_copies:
                    print(f'Copying {len(sdk_copies)} S3 objects with SDK multipart/encoding support', flush=True)
                    from boto3.s3.transfer import TransferConfig
                    transfer_config = TransferConfig(multipart_threshold=MAX_SINGLE_COPY,
                                                     multipart_chunksize=128 * 1024**2,
                                                     max_concurrency=4)
                    def copy_with_sdk(item):
                        key, src, size = item
                        src_bucket, src_key = s3_parts(src)
                        copy_source = {'Bucket': src_bucket, 'Key': src_key}
                        if size > MAX_SINGLE_COPY:
                            client.copy(copy_source, bucket, prefix + key, Config=transfer_config)
                        else:
                            client.copy_object(Bucket=bucket, Key=prefix + key,
                                               CopySource=copy_source)
                    with ThreadPoolExecutor(max_workers=min(workers, 4)) as executor:
                        for _ in executor.map(copy_with_sdk, sdk_copies):
                            pass
                completed += len(batch)
                print(f'Secondary s5cmd copied {completed}/{count} objects', flush=True)

            print('Verifying archive object keys and sizes through S3 listing', flush=True)
            for obj in objects(client, bucket, prefix):
                key = obj['Key'][len(prefix):]
                expected_size = manifest.execute('SELECT size FROM files WHERE key=?', (key,)).fetchone()
                if expected_size is None:
                    continue  # Keep files written by other secondary requests.
                elif expected_size[0] != obj['Size']:
                    raise RuntimeError('Archived object size mismatch: ' + key)
                else:
                    manifest.execute('UPDATE files SET seen=1 WHERE key=?', (key,))
            if manifest.execute('SELECT 1 FROM files WHERE seen=0 LIMIT 1').fetchone():
                raise RuntimeError('Archive verification failed: missing objects')
            print(f'Secondary archive complete: {count} objects, {total} bytes', flush=True)
            return total

