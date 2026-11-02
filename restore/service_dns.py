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

"""Start the pinned fresh-cluster DNS before restoring backends with service names."""
import json,os,socket,struct,subprocess,time
import ipaddress
from pathlib import Path
from lib import require,private_json

def dns_question(identifier):
    name=b''.join(bytes([len(label)])+label for label in b'kubernetes.default.svc.cluster.local'.split(b'.'))+b'\0'
    return struct.pack('!HHHHHH',identifier,0x0100,1,0,0,0)+name+struct.pack('!HH',1,1)

def valid_dns_response(request,response):
    if len(response)<len(request):return False
    identifier,flags,questions,answers,_,_=struct.unpack('!HHHHHH',response[:12])
    return identifier==struct.unpack('!H',request[:2])[0]and flags&0x8000!=0 and flags&15==0 and questions==1 and answers>0 and response[12:len(request)]==request[12:]

def bound_dns_endpoint(pods,slices):
    ready={p['metadata']['uid']:p for p in pods if p['metadata']['namespace']=='kube-system'and p.get('status',{}).get('phase')=='Running'and any(c['type']=='Ready'and c['status']=='True'for c in p['status'].get('conditions',[]))}
    addresses=[]
    for row in slices:
        require(row['metadata']['namespace']=='kube-system'and row['metadata']['labels'].get('kubernetes.io/service-name')=='kube-dns','unexpected_dns_endpoint_slice')
        for endpoint in row.get('endpoints')or[]:
            if endpoint.get('conditions',{}).get('ready')is not True:continue
            ref=endpoint.get('targetRef',{});pod=ready.get(ref.get('uid'))
            require(pod is not None and ref.get('kind')=='Pod'and ref.get('name')==pod['metadata']['name'],'dns_endpoint_pod_binding_mismatch')
            require(endpoint['addresses']==[pod['status']['podIP']],'dns_endpoint_address_binding_mismatch')
            address=endpoint['addresses'][0];require(ipaddress.ip_address(address)in ipaddress.ip_network('10.54.0.0/16'),'dns_endpoint_outside_target_pods');addresses.append(address)
    require(addresses,'ready_dns_endpoint_required')
    return sorted(set(addresses))[0]

def enable_dns(path,target,kube,run):
    path=Path(path);config=private_json(path,0)
    require(set(config['disable'])=={'coredns','traefik','servicelb','local-storage','metrics-server'},'fresh_target_addons_must_be_disabled_before_dns_preparation')
    config['disable']=['traefik','servicelb','local-storage','metrics-server']
    path.write_text(json.dumps(config,indent=2));path.chmod(0o600)
    run('start-isolated-target-dns',['systemctl','restart','k3s.service'],timeout=240)
    deadline=time.monotonic()+180
    while True:
        result=subprocess.run(kube+['get','namespace/kube-system','-ojson'],capture_output=True,timeout=20)
        if result.returncode==0:
            require(json.loads(result.stdout)['metadata']['uid']==target['clusterUid'],'dns_preparation_changed_cluster_uid');break
        require(time.monotonic()<deadline,'dns_preparation_api_timeout');time.sleep(2)
    run('dns-node-ready',kube+['wait','node/'+target['hostname'],'--for=condition=Ready','--timeout=180s'])
    deadline=time.monotonic()+120
    while True:
        result=subprocess.run(kube+['-n','kube-system','get','configmap/coredns','-ojson'],capture_output=True,timeout=20)
        if result.returncode==0:cm=json.loads(result.stdout);break
        require(time.monotonic()<deadline,'dns_config_registration_timeout');time.sleep(2)
    if 'NodeHosts'not in cm.get('data',{}):
        patch={'data':{'NodeHosts':target['address']+' '+target['hostname']+'\n'}}
        run('dns-missing-nodehosts',kube+['-n','kube-system','patch','configmap/coredns','--type=merge','-p',json.dumps(patch)])
    run('isolated-dns-ready',kube+['-n','kube-system','rollout','status','deployment/coredns','--timeout=180s'])
    service=json.loads(run('isolated-dns-service',kube+['-n','kube-system','get','service/kube-dns','-ojson']))
    require(service['spec']['clusterIP']=='10.55.0.10'and{(p['port'],p['protocol'])for p in service['spec']['ports']} >= {(53,'UDP'),(53,'TCP')},'target_dns_service_binding_mismatch')
    deadline=time.monotonic()+30;attempt=0
    while True:
        attempt+=1
        pods=json.loads(run('isolated-dns-pods-'+str(attempt),kube+['-n','kube-system','get','pods','-l','k8s-app=kube-dns','-ojson']))['items']
        slices=json.loads(run('isolated-dns-endpoint-slices-'+str(attempt),kube+['-n','kube-system','get','endpointslices','-l','kubernetes.io/service-name=kube-dns','-ojson']))['items']
        try:address=bound_dns_endpoint(pods,slices);break
        except ValueError as error:
            if str(error)!='ready_dns_endpoint_required'or time.monotonic()>=deadline:raise
            time.sleep(1)
    deadline=time.monotonic()+45
    while True:
        identifier=int.from_bytes(os.urandom(2),'big');question=dns_question(identifier)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM)as client:
            client.settimeout(3)
            # Without a host default route, Linux cannot initially route the
            # virtual service address before OUTPUT DNAT. Query its exact ready
            # pod endpoint instead; keep Service/EndpointSlice/Pod UID binding.
            try:client.sendto(question,(address,53));response,_=client.recvfrom(4096);valid=valid_dns_response(question,response)
            except OSError:valid=False
        if valid:break
        require(time.monotonic()<deadline,'target_service_dns_query_failed');time.sleep(2)
    return {'dnsReadyBeforeNativeRestore':True,'localServiceDnsQueryPassed':True,'clusterUidUnchanged':True,'queryTransport':'verified-coredns-pod-endpoint'}
