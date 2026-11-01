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

def reconcile_anonymous_web_access(client):
    """Replace all legacy anonymous grants with configured public buckets."""
    web_buckets, private_buckets = _configured_storage_buckets()
    # Delete the identity before adding grants: legacy configurations may
    # contain both an unscoped grant and stale bucket-scoped grants.
    if not client.delete_user("anonymous"):
        raise RuntimeError("Could not revoke existing anonymous S3 grants")
    safe_web_buckets = web_buckets - private_buckets
    if not client.add_anonymous_access(safe_web_buckets):
        raise RuntimeError("Could not grant public access to web buckets")
    if web_buckets & private_buckets:
        raise ValueError("Public and private S3 bucket names overlap")
    # Native shell commands may return success even when an operation failed.
    # Read back the persisted policy before accepting reconciliation.
    client.verify_anonymous_access(safe_web_buckets)
