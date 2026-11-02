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

"""Count native insert rows while preserving L0 delete-only segments for restore."""
import re


def segment_row_count(segment):
    value = segment.get('num_of_rows')
    if value is None:
        if segment.get('is_l0') is True and not segment.get('binlogs') and segment.get('deltalogs'):
            return 0
        raise ValueError('Missing insert row count outside a delete-only L0 segment')
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ValueError('Invalid native segment row count')
    return int(value)


def verify_visible_rows(collection,rows,statistics):
    """Insert logs include obsolete upsert versions; delete logs change visibility.

    Native artifact hashes prove the transferred log bytes. This check compares
    visible query coverage with the server's logical statistics, not a missing
    source logical fingerprint. Application acceptance remains a separate gate.
    """
    segments=[s for p in collection.get('partition_backups',[])for s in p.get('segment_backups',[])]
    inserts=sum(segment_row_count(s)for s in segments)
    deleted=any(s.get('deltalogs')for s in segments)
    count=statistics.get('rowCount')
    if isinstance(count,bool)or not isinstance(count,(int,str))or not re.fullmatch(r'[0-9]+',str(count)):
        raise ValueError('Invalid restored Milvus logical row statistic')
    if not isinstance(rows,list)or len(rows)!=int(count)or len(rows)>inserts:
        raise ValueError('Restored Milvus visible query coverage differs from collection statistics')
    if len(rows)!=inserts and not deleted:
        raise ValueError('Milvus row reduction lacks native delete log evidence')
    return {'nativeInsertRows':inserts,'restoredVisibleRows':len(rows),'nativeDeleteLogsPresent':deleted,
            'sourceLogicalFingerprintRecorded':False}
