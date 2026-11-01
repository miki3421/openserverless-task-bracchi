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
