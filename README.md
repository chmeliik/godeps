# godeps

An investigation into Go's behavior when it comes to downloading/vendoring dependencies.
The primary goal is to find the most reliable algorithm for identifying dependencies in
either case.

## Preparation

Prepare a venv, install the CLI in editable mode:

```shell
make venv
source venv/bin/activate
```

Get the example Go projects for testing:

```shell
make submodules
```

## Usage

Identify the dependencies of a module in various ways:

```shell
$ godeps -m managed-gitops/backend
godeps: downloading and identifying dependencies
godeps: writing download.txt
godeps: writing gomodcache.txt
godeps: writing listdeps_all.txt
godeps: writing listdeps_threedot.txt
godeps: diffing downloaded modules: perfect match
```

Specify a GOMODCACHE directory to re-use between multiple runs:

```shell
godeps -m sandboxed-containers-operator -c ./gocache
```

*Note: don't re-use the same GOMODCACHE for two different modules, otherwise the
reported results will be inaccurate.*

Use `go mod vendor` rather than `go mod download`:

```shell
$ godeps -m managed-gitops/backend --vendor
godeps: identifying vendored dependencies
godeps: writing vendor.txt
godeps: writing vendor_with_unused.txt
godeps: diffing vendor dirs: identified x actual
---
+++
@@ -40,7 +40,6 @@
 github.com/josharian/intern
 github.com/json-iterator/go
 github.com/kcp-dev/apimachinery
-github.com/kcp-dev/controller-runtime
 github.com/kcp-dev/controller-runtime-example
 github.com/kcp-dev/kcp/pkg/apis
 github.com/kcp-dev/logicalcluster/v2
@@ -92,6 +91,7 @@
 k8s.io/kube-openapi
 k8s.io/utils
 mellium.im/sasl
+sigs.k8s.io/controller-runtime
 sigs.k8s.io/json
 sigs.k8s.io/structured-merge-diff/v4
 sigs.k8s.io/yaml
```

*Note: the difference is due to a [replace directive][controller-runtime-replace]. The path
in the vendor/ directory corresponds to the original name, the algorithm that parses
modules.txt reports the final name.*

[controller-runtime-replace]: https://github.com/redhat-appstudio/managed-gitops/blob/588d1d2f204c537e89416ffc2cec5e9ea51297eb/backend/go.mod#L112
