# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Preserve exact legacy StatefulSet pod FQDNs using dynamic Kubernetes DNS."""
p=Promotion('legacy-pod-dns')
saved=json.loads((p.root/'native-checkpoint/native-resources.private.json').read_text())
services={s['metadata']['name']:s for s in saved['services']}
pairs=[]
for row in saved['pods']:
    spec=row['object']['spec'];hostname=spec.get('hostname');service=spec.get('subdomain')
    if not hostname or service not in services or services[service]['spec'].get('clusterIP')!='None':continue
    target=hostname.replace('nuvolaris','openserverless');target_service=service.replace('nuvolaris','openserverless')
    current=p.get('validate-current-pod-'+target,'pod/'+target,'openserverless')
    if current['spec'].get('hostname')!=target or current['spec'].get('subdomain')!=target_service:
        raise ValueError('Legacy stable pod identity cannot be mapped')
    old=hostname+'.'+service+'.nuvolaris.svc.cluster.local'
    new=target+'.'+target_service+'.openserverless.svc.cluster.local'
    pairs.append((old,new))
if not pairs:raise ValueError('Expected legacy stable pod DNS coverage')
found=subprocess.run(p.k+['-n','kube-system','get','configmap/coredns-custom','--ignore-not-found','-ojson'],capture_output=True,check=True)
data=json.loads(found.stdout).get('data',{}) if found.stdout.strip() else {}
rules=''.join('rewrite stop name exact '+old+'. '+new+'.\n' for old,new in sorted(pairs))
data['ops-legacy-pods.override']=rules
p.apply('install-exact-pod-dns-compatibility',[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'coredns-custom','namespace':'kube-system'},'data':data}])
p.run('reload-coredns-compatibility',p.k+['-n','kube-system','rollout','restart','deployment/coredns'])
p.run('coredns-compatibility-ready',p.k+['-n','kube-system','rollout','status','deployment/coredns','--timeout=120s'],timeout=150)
code='PAIRS='+repr(pairs)+'\n'+'''import socket,json,time
rows=[]
for old,new in PAIRS:
 deadline=time.monotonic()+45
 while True:
  try:
   a={v[4][0] for v in socket.getaddrinfo(old,None,socket.AF_INET)}
   b={v[4][0] for v in socket.getaddrinfo(new,None,socket.AF_INET)}
   if a and a==b:break
  except OSError:pass
  if time.monotonic()>=deadline:raise ValueError('Legacy pod DNS endpoint mismatch: '+old)
  time.sleep(2)
 rows.append({'old':old,'new':new,'sameCurrentEndpoint':True})
print(json.dumps(rows))
'''
verified=json.loads(p.run('verify-old-and-new-incluster-dns',p.k+['-n','openserverless','exec','-i','openserverless-operator-0','--','python3','-'],input=code.encode()).stdout)
p.state['exactPodDnsAliasesVerified']=verified;p.save()
print(json.dumps({'stablePodNamesMapped':len(verified),'exactNamesOnly':True,'addressesFollowKubernetesServiceDiscovery':True,'applicationCodeAndCredentialsRewritten':False,'originalApplicationCodeRewritten':False}))
