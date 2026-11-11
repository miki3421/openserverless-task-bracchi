# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""Fixed SSH helper: inspect, compare-and-replace, and wait; no shell payloads."""
import fcntl,hashlib,json,os,subprocess,sys,tempfile,time
from pathlib import Path
PATH=Path('/var/lib/rancher/k3s/server/manifests/traefik-config.yaml')
HISTORY_PATH=Path('/var/lib/rancher/ops-traefik-history')
LOCK_PATH=Path('/run/lock/ops-traefik-pin.lock')
def kube(kind,name):
    p=subprocess.run(['k3s','kubectl','-n','kube-system','get',kind+'/'+name,'--ignore-not-found','-o','json','--request-timeout=20s'],capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Cannot inspect K3s '+kind)
    return json.loads(p.stdout) if p.stdout.strip() else None
def snapshot():
    active=subprocess.run(['systemctl','is-active','--quiet','k3s']).returncode==0
    unit=subprocess.run(['systemctl','show','-p','LoadState','--value','k3s'],capture_output=True,text=True)
    installed=unit.stdout.strip() not in ('','not-found') or Path('/var/lib/rancher/k3s/server/db').exists()
    if not active and installed:
        raise RuntimeError('Existing K3s must be active to inspect live chart precedence')
    raw=PATH.read_text() if PATH.exists() else None
    others=[]
    if PATH.parent.exists():
        for p in PATH.parent.rglob('*'):
            if p==PATH or not p.is_file() or p.suffix not in ('.yaml','.yml','.json'):continue
            if p.stat().st_size>2*1024*1024:raise RuntimeError('Oversize manifest cannot be safely inspected')
            s=p.read_text()
            if 'HelmChartConfig' in s and 'traefik' in s:others.append({'name':p.name,'content':s})
    return {'active':active,'file':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None,'chart':kube('helmchart','traefik') if active else None,'live':kube('helmchartconfig','traefik') if active else None,'otherManifests':others}
def signature(state):
    return [state['active'],state['sha256'],(state['chart'] or {}).get('spec'),(state['live'] or {}).get('spec'),state['otherManifests']]
def wait(image):
    if subprocess.run(['systemctl','is-active','--quiet','k3s']).returncode!=0:return
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        d=kube('deployment','traefik')
        if d:
            containers=d['spec']['template']['spec']['containers'];status=d.get('status',{});n=d['spec'].get('replicas',1)
            if n>0 and any(c['name']=='traefik' and c['image']==image for c in containers) and status.get('observedGeneration',0)>=d['metadata']['generation'] and status.get('replicas',0)==n and status.get('updatedReplicas',0)==n and status.get('readyReplicas',0)>=n and status.get('availableReplicas',0)>=n:
                return
        time.sleep(2)
    raise RuntimeError('Timed out waiting for the pinned Traefik deployment')
def main():
    request=json.load(sys.stdin)
    if request['operation']=='inspect':print(json.dumps(snapshot()));return
    if request['operation'] not in ('commit','wait'):raise RuntimeError('Unknown operation')
    with open(LOCK_PATH,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=snapshot()
        if signature(state)!=signature(request['expected']):sys.exit(73)
        if request['operation']=='commit':
            if state['file'] is not None:
                history=HISTORY_PATH;history.mkdir(mode=0o700,parents=True,exist_ok=True)
                backup=history/(state['sha256']+'.yaml')
                if not backup.exists():
                    fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    with os.fdopen(fd,'w') as f:f.write(state['file'])
            PATH.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix='.ops-traefik-',suffix='.tmp',dir=PATH.parent)
            try:
                with os.fdopen(fd,'w') as f:f.write(request['content']);f.flush();os.fsync(f.fileno())
                os.chmod(name,0o600);os.replace(name,PATH)
            finally:
                if os.path.exists(name):os.unlink(name)
    wait(request['image'])
if __name__=='__main__':
    try:main()
    except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
