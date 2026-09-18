import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
import empty_secondary as empty
import secondary_archive as archive
import finish_secondary

class EmptyTests(unittest.TestCase):
    def setUp(self):
        self.request = {'validator': True, 'app': {'service': 'test-validator'}}
        self.source = 's3://test/tasks/' + 'a' * 24 + '/secondary'
        self.client = Mock()
        self.client.list_objects_v2.return_value = {}
        self.code = b'reviewed inline-only validator'
        self.product = {'errors': [], 'brainlife': [{'type': 'plotly'}]}
        self.client.get_object.side_effect = lambda **kw: {'Body': io.BytesIO(self.code if kw['Key'].endswith('validate.py') else json.dumps(self.product).encode())}
        self.enterContext(patch.object(empty, 's3_client', return_value=self.client))
        self.enterContext(patch.dict(empty.INLINE_ONLY_VALIDATORS, {'test-validator': hashlib.sha256(self.code).hexdigest()}))

    def test_confirmed_inline_only_and_changed_code(self):
        self.assertTrue(empty.confirmed_empty(self.request, self.source))
        self.code = b'new version'
        self.assertFalse(empty.confirmed_empty(self.request, self.source))

    def test_unknown_missing_and_groupanalysis_not_skipped(self):
        self.assertFalse(empty.confirmed_empty({'validator':True}, self.source))
        self.assertFalse(empty.confirmed_empty({'datatype':{}}, self.source))
        self.assertFalse(empty.confirmed_empty(self.request, 's3://test/tasks/'+'a'*24+'/output'))
        self.client.list_objects_v2.return_value = {'Contents':[{'Key':'secondary/x.png'}]}
        self.assertFalse(empty.confirmed_empty(self.request, self.source))

    def test_failed_validator_and_access_failure(self):
        self.product['errors'] = ['invalid data']
        self.assertFalse(empty.confirmed_empty(self.request, self.source))
        self.client.get_object.side_effect = ClientError({'Error':{'Code':'AccessDenied'}}, 'GetObject')
        with self.assertRaises(ClientError): empty.confirmed_empty(self.request, self.source)
        self.client.get_object.side_effect = ClientError({'Error':{'Code':'NoSuchKey'}}, 'GetObject')
        self.assertFalse(empty.confirmed_empty(self.request, self.source))

    def test_local_empty_and_success_result_without_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=os.getcwd();os.chdir(tmp)
            try:
                Path('secondary').mkdir()
                req=dict(self.request,src='secondary',group_id=1,instance_id='instance',task_id='task',subdir='output/secondary')
                Path('config.json').write_text(json.dumps({'requests':[req]}))
                with patch.dict(os.environ,SECONDARY_ARCHIVE_S3_BUCKET='test'), patch.object(archive,'archive_directory') as copy:
                    archive.main();finish_secondary.main();copy.assert_not_called()
                result=json.loads(Path('secondary-archive-result.json').read_text())
                self.assertTrue(result['complete']);self.assertEqual(result['requests'][0]['status'],'empty')
                Path('secondary/file').write_text('data')
                self.assertFalse(empty.confirmed_empty(req,'secondary'))
                self.assertFalse(empty.confirmed_empty(req,'missing'))
            finally: os.chdir(old)

    def test_reviewed_quartz_mask_version_without_s3_task_files(self):
        req = {'validator': True, 'finish_date': '2026-09-18T22:47:10.556Z',
               'app': {'service': 'brainlife/validator-neuro-mask',
                       'commit_id': 'b209f0b137fd564a335505ad6873d063fd0eaa38'}}
        self.assertTrue(empty.confirmed_empty(req, self.source))
        self.client.get_object.assert_not_called()
        req.pop('finish_date')
        self.assertFalse(empty.confirmed_empty(req, self.source))
        req['finish_date'] = '2026-09-18T22:47:10.556Z'
        req['app']['commit_id'] = 'unreviewed-version'
        self.assertFalse(empty.confirmed_empty(req, self.source))
        req['app']['commit_id'] = 'b209f0b137fd564a335505ad6873d063fd0eaa38'
        self.client.list_objects_v2.return_value = {'Contents': [{'Key': 'secondary/x.png'}]}
        self.assertFalse(empty.confirmed_empty(req, self.source))
        self.client.list_objects_v2.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'ListObjectsV2')
        with self.assertRaises(ClientError):
            empty.confirmed_empty(req, self.source)
