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

"""Wait for ordinary startup; fail immediately on a recorded handler/container error."""
p=Promotion('wait-platform-090')
required=['openwhisk','couchdb','redis','postgres','mongodb','seaweedfs','milvus','etcd','static','whisk-system']
deadline=time.monotonic()+600
while True:
    whisk=p.get('observe-platform','whisk/controller','openserverless')
    progress=whisk.get('status',{}).get('kopf',{}).get('progress',{})
    if any(x.get('failure') or x.get('message') for x in progress.values()):
        raise ValueError('Operator handler reported an error; stop migration')
    pods=p.get('observe-platform-pods','pods','openserverless')['items']
    for pod in pods:
        if pod['status'].get('phase') in ['Failed','Unknown']:
            raise ValueError('Platform pod failed: '+pod['metadata']['name'])
        for status in pod['status'].get('containerStatuses',[])+pod['status'].get('initContainerStatuses',[]):
            waiting=status.get('state',{}).get('waiting',{}).get('reason')
            if waiting in ['CrashLoopBackOff','ErrImagePull','ImagePullBackOff','CreateContainerConfigError','InvalidImageName']:
                raise ValueError('Platform container startup error: '+pod['metadata']['name']+' '+waiting)
    state=whisk.get('status',{}).get('whisk_create',{})
    for component in required:
        if state.get(component)=='error':raise ValueError('Platform component failed: '+component)
    if all(state.get(k)=='on' for k in required) and state.get('controller')=='Ready':
        break
    if time.monotonic()>=deadline:
        raise ValueError('Platform startup deadline exceeded')
    time.sleep(3)
p.state['normalStartupReady']=True;p.save()
print(json.dumps({'componentsReady':required,'controllerReady':True,'operatorHandlerErrors':False,'originalApplicationCodeRewritten':False}))
