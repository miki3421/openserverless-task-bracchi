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

"""Preserve source runtime kinds while adding mandatory target system runtimes."""
import copy

def merge_system_runtimes(source, target):
    merged=copy.deepcopy(source)
    by_kind={entry['kind']:entry for family in merged['runtimes'].values() for entry in family}
    added=[]
    for family,entries in target['runtimes'].items():
        for entry in entries:
            if not entry['kind'].endswith(':sys'):continue
            candidate=copy.deepcopy(entry);candidate['default']=False
            if entry['kind'] in by_kind:
                if by_kind[entry['kind']]!=candidate:raise ValueError('Source runtime conflicts with required target system runtime')
                continue
            merged['runtimes'].setdefault(family,[]).append(candidate);by_kind[candidate['kind']]=candidate;added.append(candidate['kind'])
    if 'python:sys' not in by_kind:raise ValueError('Required target Python system runtime missing')
    return merged,added
