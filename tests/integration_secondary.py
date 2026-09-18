"""Exercise real s5cmd and both secondary entry points against isolated S3."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import boto3
from moto.server import ThreadedMotoServer


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    server=ThreadedMotoServer(ip_address='127.0.0.1',port=0,verbose=False);server.start()
    try:
        host,port=server.get_host_and_port();endpoint=f'http://{host}:{port}'
        client=boto3.client('s3',endpoint_url=endpoint,region_name='us-east-1',aws_access_key_id='testing',aws_secret_access_key='testing')
        client.create_bucket(Bucket='secondary-test')
        repo=Path(__file__).resolve().parents[1]
        env={k:v for k,v in os.environ.items() if not k.startswith(('AWS_','S3_','SECONDARY_','BRAINLIFE_'))}
        env.update(AWS_ACCESS_KEY_ID='testing',AWS_SECRET_ACCESS_KEY='testing',AWS_REGION='us-east-1',AWS_EC2_METADATA_DISABLED='true',S3_ENDPOINT_URL=endpoint,SECONDARY_ARCHIVE_S3_BUCKET='secondary-test')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();(source/'x.png').write_bytes(b'png bytes');(source/'space [1]*.json').write_bytes(b'{}');(source/'empty').mkdir()
            env['SECONDARY_ARCHIVE']=str(root/'osiris')
            base=dict(group_id=25212,instance_id='instance',task_id='task',subdir='output/secondary',src=str(source),validator=True)
            def run(requests, success=True, legacy=False):
                (root/'config.json').write_text(json.dumps({'requests':requests}))
                command=[sys.executable,str(repo/('legacy_archive.py' if legacy else 'secondary_archive.py'))]
                proc=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=60)
                assert (proc.returncode==0)==success,proc.stdout+proc.stderr
                if success and not legacy:
                    proc=subprocess.run([sys.executable,str(repo/'finish_secondary.py')],cwd=root,env=env,capture_output=True,text=True,timeout=60)
                    assert proc.returncode==0,proc.stdout+proc.stderr
                    assert json.loads((root/'secondary-archive-result.json').read_text())['complete']
                if not success:assert not (root/'secondary-archive-result.json').exists()
            prefix='secondary-archive/25212/instance/task/output/secondary/'
            client.put_object(Bucket='secondary-test',Key=prefix+'unrelated.txt',Body=b'keep')
            run([base]);run([base])
            assert client.get_object(Bucket='secondary-test',Key=prefix+'x.png')['Body'].read()==b'png bytes'
            assert client.head_object(Bucket='secondary-test',Key=prefix+'empty/')['ContentLength']==0
            assert client.get_object(Bucket='secondary-test',Key=prefix+'unrelated.txt')['Body'].read()==b'keep'
            assert not (root/'osiris').exists(), 'Preview should not be mirrored'
            # Root folder markers, spaces, and S3 copies preserve the same layout.
            for name,data in [('',b''),('x.png',b'new png'),('space [1]*.json',b'{}')]:
                client.put_object(Bucket='secondary-test',Key='tasks/id/secondary/'+name,Body=data)
            remote=dict(base,src='s3://secondary-test/tasks/id/secondary')
            run([remote]);assert client.get_object(Bucket='secondary-test',Key=prefix+'x.png')['Body'].read()==b'new png'
            assert client.get_object(Bucket='secondary-test',Key=prefix+'unrelated.txt')['Body'].read()==b'keep'
            # Group analysis gets both verified S3 files and the compatibility copy.
            ga=dict(base,subdir='stats',validator=False,datatype={'name':'stats'})
            run([base,ga])
            assert (root/'osiris/25212/instance/task/stats/x.png').read_bytes()==b'png bytes'
            result=json.loads((root/'secondary-archive-result.json').read_text())
            assert [r['groupanalysis_compatibility_copy'] for r in result['requests']]==[False,True]
            run([dict(base,src=str(root/'missing'))],success=False)
            run([dict(base,subdir='../escape')],success=False)
            # The rollback branch needs neither the container nor AWS credentials.
            run([base],legacy=True)
            assert (root/'osiris/25212/instance/task/output/secondary/x.png').read_bytes()==b'png bytes'
            print('PASS: real s5cmd upload/copy, retry merge, folder markers, unusual keys, multiple requests, notebook mirror, failures, and legacy rollback')
    finally:server.stop()

if __name__=='__main__':main()
