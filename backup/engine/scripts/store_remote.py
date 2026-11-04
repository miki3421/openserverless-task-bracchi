"""Authenticated S3 transport and the existing Restic repository format."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from store_common import require,read,regular,digest,command

class Destination:
    def __init__(self,c):
        import boto3
        from botocore.config import Config
        self.cfg=c;self.credentials=read(c['paths']['credentials'],c['host']['uid'])
        require(set(self.credentials)=={'accessKey','secretKey'} and all(isinstance(v,str) and len(v)>=16 for v in self.credentials.values()),'invalid_s3_credentials')
        self.client=boto3.client('s3',endpoint_url=c['s3']['endpoint'],region_name='us-east-1',
            aws_access_key_id=self.credentials['accessKey'],aws_secret_access_key=self.credentials['secretKey'],
            verify=c['paths']['ca'],config=Config(signature_version='s3v4',s3={'addressing_style':'path'},connect_timeout=10,read_timeout=120))
        self.bucket=c['s3']['bucket'];self.prefix=c['s3']['prefix']+'/'

    def key(self,key):
        require(key and not key.startswith('/') and '..' not in key.split('/') and '\\' not in key,'unsafe_s3_key')
        return self.prefix+key

    def list(self,prefix):
        result=[]
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket,Prefix=self.key(prefix)):
            result.extend(page.get('Contents',[]));require(len(result)<=100000,'object_listing_budget')
        return result

    def put_json(self,key,value):
        data=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        require(len(data)<=16*1024**2,'object_metadata_budget')
        self.client.put_object(Bucket=self.bucket,Key=self.key(key),Body=data,ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),ContentType='application/json')
        require(self.client.get_object(Bucket=self.bucket,Key=self.key(key))['Body'].read()==data,'object_roundtrip_mismatch')
        return hashlib.sha256(data).hexdigest()

    def get_json(self,key,expected=None):
        response=self.client.get_object(Bucket=self.bucket,Key=self.key(key));require(response['ContentLength']<=16*1024**2,'object_metadata_budget')
        data=response['Body'].read()
        if expected:require(hashlib.sha256(data).hexdigest()==expected,'object_manifest_changed')
        from store_common import unique
        return json.loads(data,object_pairs_hook=unique)

    def upload_repository(self,source,prefix):
        source=Path(source);require(source.is_dir() and not source.is_symlink(),'repository_directory_required')
        require(not self.list(prefix+'/'),'new_s3_repository_required')
        records=[]
        for path in sorted(source.rglob('*')):
            require(not path.is_symlink(),'repository_symlink_rejected')
            if path.is_dir():continue
            regular(path,os.getuid())
            name=str(path.relative_to(source));require(name.split('/')[0] in {'config','data','index','keys','snapshots','locks'},'unexpected_repository_file')
            require(not name.startswith('locks/'),'unlocked_repository_required')
            h=digest(path);size=path.stat().st_size
            self.client.upload_file(str(path),self.bucket,self.key(prefix+'/'+name),ExtraArgs={'Metadata':{'sha256':h}})
            head=self.client.head_object(Bucket=self.bucket,Key=self.key(prefix+'/'+name))
            require(head['ContentLength']==size and head.get('Metadata',{}).get('sha256')==h,'repository_upload_mismatch')
            records.append({'file':name,'bytes':size,'sha256':h})
        require(records and any(r['file']=='config'for r in records),'incomplete_repository_upload')
        return records

    def download_repository(self,prefix,records,target):
        target=Path(target);require(not target.exists(),'fresh_repository_download_required');target.mkdir(mode=0o700)
        expected={self.key(prefix+'/'+r['file']) for r in records}
        actual={x['Key'] for x in self.list(prefix+'/')}
        require(actual==expected,'repository_object_set_changed')
        for r in records:
            name=r['file'];require(name and '..' not in name.split('/') and not name.startswith('/') and '\\' not in name,'unsafe_repository_path')
            p=target/name;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with p.open('xb')as out:
                p.chmod(0o600);self.client.download_fileobj(self.bucket,self.key(prefix+'/'+name),out)
            require(p.stat().st_size==r['bytes'] and digest(p)==r['sha256'],'repository_download_mismatch')
        return target

class RemoteRestic:
    def __init__(self,destination,source_cfg,prefix,logs):
        self.d=destination;self.cfg=source_cfg;self.logs=Path(logs);self.logs.mkdir(mode=0o700,parents=True,exist_ok=True)
        s=source_cfg['kind']['storage'];self.binary=regular(s['restic_binary'],os.getuid(),False)
        require(digest(self.binary)==s['restic_sha256'],'restic_checksum_mismatch');regular(s['password_file'],os.getuid())
        self.url='s3:'+destination.cfg['s3']['endpoint']+'/'+destination.bucket+'/'+destination.key(prefix)
        self.common=[str(self.binary),'--repo',self.url,'--password-file',s['password_file'],'--no-cache','--cacert',destination.cfg['paths']['ca']]
        self.env={k:os.environ[k]for k in ['PATH','HOME','LANG','LC_ALL']if k in os.environ}
        self.env.update(AWS_ACCESS_KEY_ID=destination.credentials['accessKey'],AWS_SECRET_ACCESS_KEY=destination.credentials['secretKey'],AWS_DEFAULT_REGION='us-east-1')

    def call(self,label,args,timeout=7200):
        return command(self.common+list(args),self.logs/(label+'-'+uuid.uuid4().hex+'.stderr'),env=self.env,timeout=timeout)

    def init(self):self.call('init',['init','--repository-version','2'])
    def check(self):self.call('check',['check','--read-data'])

    def process(self,args):
        err=(self.logs/('stream-'+uuid.uuid4().hex+'.stderr')).open('xb');os.chmod(err.name,0o600)
        p=subprocess.Popen(self.common+list(args),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,env=self.env)
        return p,err
