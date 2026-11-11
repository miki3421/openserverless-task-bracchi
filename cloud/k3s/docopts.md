<!---
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->
# Tasks  `ops cloud k3s`

Create and Manage K3S cluster

## Synopsis

```text
Usage:
  k3s create <server> [<user>]
  k3s delete <server> [<user>]
  k3s info
  k3s kubeconfig <server> [<user>]
  k3s registry <server> [<user>]
  k3s traefik <server> [<user>]
  k3s status
```

## Commands

```
  create     create a k3s with ssh in <server> using <user> with sudo
  delete     uninstall k3s with ssh in <server> using <username> with sudo
  info       info on the server
  kubeconfig recover the kubeconfig from a k3s server <server> with user <username>
  registry   configure the scoped OPS internal registry mirror on a k3s server
  traefik    reconcile the qualified ingress image, preserving existing configuration
  status     status of the server
```

`create` configures the same mirror automatically before installing K3s. The
mirror applies only to `openserverless-registry-svc:5000`; public registries
remain unchanged. Existing custom `/etc/rancher/k3s/registries.yaml` files are
never overwritten. When `registry` installs the file on a running server, K3s
is restarted so that containerd loads the new configuration.

`traefik` uses the immutable `config.images.traefik` catalog entry and a separate
HelmChartConfig file. It preserves existing nodePorts and unrelated values, and
keeps an atomic-write rollback copy outside the manifests directory. Fresh
configurations default to ports 30080/30081. Conflicting live/file configuration,
higher-precedence image selectors, marketplace images and secret-valued Helm
overrides require separate compatibility review and are not silently replaced.
YAML scalars that JSON cannot preserve exactly are rejected before mutation.
The command waits for the selected image to converge on a running K3s server.
For a fresh configuration only, `K3S_TRAEFIK_HTTP_NODEPORT` / `HTTPS_NODEPORT`
override `TRAEFIK_HTTP_NODEPORT` / `HTTPS_NODEPORT`; existing ports are retained.
