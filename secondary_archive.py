"""Archive secondary requests to S3, retaining optional notebook compatibility."""
import json
import os
from pathlib import Path
import re

from s3_transfer import archive_directory, safe_relative


def destination_path(request):
    components = []
    for field in ('group_id', 'instance_id', 'task_id'):
        value = str(request.get(field, ''))
        if not value or value in ('.', '..') or '/' in value or '\\' in value:
            raise ValueError('Invalid secondary ' + field)
        components.append(value)
    subdir = safe_relative(request['subdir'])
    if not subdir:
        raise ValueError('Secondary subdir must not be empty')
    return '/'.join(components + [subdir])


def requests_from_config(config):
    requests = []
    if 'validator_task' in config:
        task = config['validator_task']
        requests.append({'src': '../' + task['_id'] + '/secondary',
                         'group_id': task['_group_id'], 'instance_id': task['instance_id'],
                         'task_id': task['deps_config'][0]['task'],
                         'subdir': task['config']['_outputs'][0]['id'] + '/secondary',
                         'validator': True})
    requests.extend(config.get('requests', []))
    return requests


def resolve_request_source(source):
    # Batch task dependencies may exist only as S3 objects, not local symlinks.
    if source.startswith('../') and not Path(source).exists():
        match = re.fullmatch(r'\.\./([0-9a-f]{24})/(.+)', source)
        if not match:
            raise ValueError('Missing secondary source: ' + source)
        rest = safe_relative(match.group(2))
        bucket = os.environ.get('BRAINLIFE_ARCHIVE_S3FS_BUCKET')
        if not bucket:
            raise ValueError('An explicit source mount bucket is required')
        return 's3://' + bucket + '/tasks/' + match.group(1) + '/' + rest
    return source


def main():
    result_path = Path('secondary-archive-result.json')
    if result_path.exists():
        result_path.unlink()
    with open('config.json') as stream:
        requests = requests_from_config(json.load(stream))
    bucket = os.environ['SECONDARY_ARCHIVE_S3_BUCKET']
    root = safe_relative(os.environ.get('SECONDARY_ARCHIVE_S3_PREFIX', 'secondary-archive'))
    if not root:
        raise ValueError('Secondary prefix root must not be empty')
    # Validate every destination before starting any writes.
    planned = [(request, destination_path(request)) for request in requests]
    results = []
    for request, destination in planned:
        prefix = root + '/' + destination + '/'
        size = archive_directory(resolve_request_source(request['src']), bucket, prefix)
        results.append({'bucket': bucket, 'prefix': prefix, 'size': size,
                        'groupanalysis_compatibility_copy': False})
    result_path.write_text(json.dumps({'requests': results}, indent=2) + '\n')


if __name__ == '__main__':
    main()
