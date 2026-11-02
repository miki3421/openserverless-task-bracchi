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

"""Fresh-target binding used only by the data-only restore promotion route."""
def promotion_binding(marker,bootstrap_uid,live_uid,namespace_uid,namespace_empty):
    if marker.get('role')!='ops-local-restore' or marker.get('applicationExecutionAllowed')is not False or marker.get('restorePromotionReady')is not True:
        raise ValueError('marked_restore_target_required')
    if marker.get('sourceClusterUid')==live_uid or marker.get('targetClusterUid')!=bootstrap_uid or bootstrap_uid!=live_uid:
        raise ValueError('restore_target_cluster_changed')
    if marker.get('nativeNamespaceUid')!=namespace_uid or not namespace_empty:
        raise ValueError('native_restore_pods_must_be_stopped')
