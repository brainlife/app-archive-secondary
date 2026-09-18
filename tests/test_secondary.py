import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import secondary_archive as archive
import finish_secondary

class SecondaryTests(unittest.TestCase):
    def test_exact_paths_and_no_duplicate_secondary(self):
        req = dict(group_id=25212, instance_id='instance', task_id='task', subdir='output/secondary')
        self.assertEqual(archive.destination_path(req), '25212/instance/task/output/secondary')
        req['subdir'] = 'freesurfer'
        self.assertEqual(archive.destination_path(req), '25212/instance/task/freesurfer')
        for field in ('group_id','instance_id','task_id','subdir'):
            bad = dict(req); bad[field] = '../escape'
            with self.assertRaises(ValueError): archive.destination_path(bad)

    def test_old_validator_format(self):
        task = {'_id':'validator','_group_id':12,'instance_id':'instance',
                'deps_config':[{'task':'parent'}], 'config':{'_outputs':[{'id':'output'}]}}
        req = archive.requests_from_config({'validator_task':task})[0]
        self.assertEqual(req['src'], '../validator/secondary')
        self.assertEqual(archive.destination_path(req), '12/instance/parent/output/secondary')

    def test_missing_batch_source_uses_explicit_bucket(self):
        with patch.dict(os.environ, {'BRAINLIFE_ARCHIVE_S3FS_BUCKET':'brainlife'}):
            self.assertEqual(archive.resolve_request_source('../'+'a'*24+'/secondary'),
                             's3://brainlife/tasks/'+'a'*24+'/secondary')
            with self.assertRaises(ValueError): archive.resolve_request_source('../../outside')

    def test_groupanalysis_copy_only_and_failure_removes_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd(); os.chdir(tmp)
            self.addCleanup(os.chdir, old)
            req = dict(group_id=12, instance_id='instance', task_id='task', subdir='output', src='input')
            requests = [dict(req, validator=True), dict(req, datatype={'name':'stats'})]
            Path('config.json').write_text(json.dumps({'requests':requests}))
            result = {'requests':[{},{}]}
            Path('secondary-archive-result.json').write_text(json.dumps(result))
            with patch.dict(os.environ, {'SECONDARY_ARCHIVE':'/legacy','SECONDARY_ARCHIVE_KEEP_GROUPANALYSIS':'1'}), \
                 patch.object(finish_secondary,'handle_request') as copy:
                finish_secondary.main()
                copy.assert_called_once_with('input','12/instance/task/output')
                self.assertTrue(json.loads(Path('secondary-archive-result.json').read_text())['complete'])
            Path('secondary-archive-result.json').write_text(json.dumps(result))
            with patch.dict(os.environ, {'SECONDARY_ARCHIVE':'/legacy'}), \
                 patch.object(finish_secondary,'handle_request', side_effect=OSError('copy failed')):
                with self.assertRaises(OSError): finish_secondary.main()
                self.assertFalse(Path('secondary-archive-result.json').exists())

    def test_runtime_failure_removes_previous_result(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / 'singularity'
            runtime.write_text('#!/bin/sh\nexit 19\n');runtime.chmod(0o755)
            (root/'secondary-archive-result.json').write_text('{"complete":true}')
            env = dict(os.environ, PATH=tmp+os.pathsep+os.environ['PATH'], SECONDARY_ARCHIVE_S3_BUCKET='brainlife')
            result = subprocess.run(['bash',str(repo/'main')],cwd=tmp,env=env,capture_output=True)
            self.assertEqual(result.returncode,19)
            self.assertFalse((root/'secondary-archive-result.json').exists())
