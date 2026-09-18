"""Recognize empty validator previews without hiding missing archive data."""
import hashlib
import json
from pathlib import Path
import re
from s3_transfer import s3_client, s3_parts

# Reviewed task snapshot: creates secondary/ but only writes an inline Plotly
# preview to product.json. Pin the code, not merely the service name: a future
# validator version may start producing files. Do not infer emptiness from plots.
INLINE_ONLY_VALIDATORS = {
    'brainlife/validator-neuro-wmc':
        'bd3b6ab18a504ba4877bffb9bbd020d01d9475180f92cbc222acf1a025f018d9',
}


# This reviewed main script only copies the mask into output/ and writes
# validation results. It never creates secondary files. Quartz does not upload
# its task scripts/product to S3, so use the dispatcher-provided exact commit
# and successful finish metadata instead of probing nonexistent S3 evidence.
NO_SECONDARY_COMMITS = {
    ('brainlife/validator-neuro-mask', 'b209f0b137fd564a335505ad6873d063fd0eaa38'),
}


def confirmed_empty(request, source):
    if not request.get('validator'):
        return False  # Never skip group-analysis data.
    if not source.startswith('s3://'):
        path = Path(source)
        return path.is_dir() and next(path.iterdir(), None) is None
    bucket, key = s3_parts(source)
    if not re.fullmatch(r'tasks/[0-9a-f]{24}/secondary/?', key):
        return False
    client = s3_client()
    listing = client.list_objects_v2(Bucket=bucket, Prefix=key.rstrip('/') + '/', MaxKeys=1)
    if listing.get('Contents'):
        return False
    app = request.get('app') or {}
    if (app.get('service'), app.get('commit_id')) in NO_SECONDARY_COMMITS and request.get('finish_date'):
        return True
    expected = INLINE_ONLY_VALIDATORS.get(app.get('service'))
    if not expected:
        return False
    task_root = key.rstrip('/').rsplit('/', 1)[0] + '/'
    def read(name):
        response = client.get_object(Bucket=bucket, Key=task_root + name)
        with response['Body'] as body:
            return body.read()
    from botocore.exceptions import ClientError
    try:
        if hashlib.sha256(read('validate.py')).hexdigest() != expected:
            return False
        product = json.loads(read('product.json'))
    except ClientError as error:
        if error.response['Error']['Code'] in ('NoSuchKey', '404', 'NotFound'):
            return False
        raise  # Access and storage failures must not look like empty outputs.
    except (ValueError, TypeError):
        return False
    return (isinstance(product, dict) and product.get('errors') == []
            and any(isinstance(item, dict) and item.get('type') == 'plotly'
                    for item in (product.get('brainlife') or [])))
