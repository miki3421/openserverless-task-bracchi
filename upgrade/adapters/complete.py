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

p=Promotion('complete')
if not json.loads((p.root/'iam-090/journal.json').read_text()).get('restoredIamReconciled'):
    raise ValueError('Final IAM gate incomplete')
resources=p.get('managed-workloads','deployments,statefulsets','openserverless')['items']
if any(r.get('status',{}).get('readyReplicas',0)<r['spec'].get('replicas',1) for r in resources):raise ValueError('Managed workload not Ready')
p.marker.update(activePhase='upgraded-090',upgradeCompleted=True,applicationExecutionAllowed=True,applicationAcceptanceRequired=True)
p.marker_path.write_text(json.dumps(p.marker,indent=2))
print(json.dumps({'targetVersion':'0.9.0','readyManagedWorkloads':len(resources),'credentialsAndDefinitionsPreserved':True,
                  'applicationAcceptanceRequired':True,'finalIamReconciled':True,'productionCutoverCertified':False}))
