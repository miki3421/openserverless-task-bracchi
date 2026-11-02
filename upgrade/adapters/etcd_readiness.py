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

"""Keep clients out of the Bitnami temporary background setup server."""
import shlex

def etcd_foreground_probe(process_exe='/proc/1/exe',binary='/opt/bitnami/etcd/bin/etcd',health='/opt/bitnami/scripts/etcd/healthcheck.sh'):
    # Setup briefly exposes a healthy background listener. Its stop helper uses
    # pgrep("^etcd"), which also matches concurrent etcdctl clients. Readiness
    # must wait until the final etcd executable replaces PID1.
    return ['sh','-ec','test '+shlex.quote(process_exe)+' -ef '+shlex.quote(binary)+'; exec '+shlex.quote(health)]
