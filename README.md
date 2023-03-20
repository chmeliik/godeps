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
godeps: writing download_plus_local_paths.txt
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

## Testing procedure

Uses [fd][fd-find] as a more convenient `find` replacement.

```shell
# write results to all module directories
fd go.mod -x godeps -m {//} -o {//} -c {//}/.gocache
fd go.mod -x godeps -m {//} -o {//} -c {//}/.gocache --vendor

# verify that download == gomodcache for all modules
fd go.mod -x diff {//}/download.txt {//}/gomodcache.txt

# verify that download_plus_local_paths == vendor for all modules
fd go.mod -x diff {//}/download_plus_local_paths.txt {//}/vendor.txt

# verify that vendor >= listdeps_all >= listdeps_threedot for all modules
fd go.mod -x diff --color=always {//}/vendor.txt {//}/listdeps_all.txt
fd go.mod -x diff --color=always {//}/listdeps_all.txt {//}/listdeps_threedot.txt
```

## Interesting findings

### The difference between \<a complete source of modules\> and listdeps\_all

```shell
$ cd managed-gitops/tests-e2e

# take modules that appear only in the left file, remove version numbers:
$ comm -23 <(sort < vendor.txt) <(sort < listdeps_all.txt) | cut -d @ -f 1
github.com/acomagu/bufpipe
github.com/Azure/go-ansiterm
github.com/Azure/go-autorest
github.com/inconshreveable/mousetrap
github.com/Microsoft/go-winio
google.golang.org/appengine

# ask Go why we need those modules:
#   -m: the input is module names, not package names
#   -vendor: exlude tests of dependencies
$ comm -23 <(sort < vendor.txt) <(sort < listdeps_all.txt) | cut -d @ -f 1 |
    xargs go mod why -m -vendor
# github.com/acomagu/bufpipe
github.com/redhat-appstudio/managed-gitops/tests-e2e
github.com/argoproj/argo-cd/v2/pkg/apis/application/v1alpha1
github.com/argoproj/argo-cd/v2/util/git
github.com/go-git/go-git/v5/utils/ioutil
github.com/acomagu/bufpipe

...
```

The `go mod why` command shows the import path between your main module and the dependency
module. When you inspect the package pairs (importer and importee) along the way, you'll
find [build constraints][go-build-constraints]:

```shell
$ grep -R '"github.com/acomagu/bufpipe"' vendor/github.com/go-git/go-git/v5/utils/ioutil \
    --files-with-matches
vendor/github.com/go-git/go-git/v5/utils/ioutil/pipe_js.go

$ grep -R '"github.com/acomagu/bufpipe"' vendor/github.com/go-git/go-git/v5/utils/ioutil \
    --files-with-matches | xargs grep -E 'go:build|\+build'
// +build js
```

You can verify that these build constraints are the reason why `go list -deps all` is not
reporting the module:

```shell
$ go list -deps all | grep '^github.com/acomagu/bufpipe$'
$ go list -deps -tags js all | grep '^github.com/acomagu/bufpipe$'
github.com/acomagu/bufpipe
```

There's no way make the `go list` command ignore build constraints, see <https://github.com/golang/go/issues/42504>.

### The difference between listdeps\_all and listdeps\_threedot

```shell
$ cd managed-gitops/tests-e2e
$ comm -23 <(sort < listdeps_all.txt) <(sort < listdeps_threedot.txt) | cut -d @ -f 1 |
    xargs go mod why -m -vendor
# github.com/containerd/containerd
github.com/redhat-appstudio/managed-gitops/tests-e2e/core
github.com/redhat-appstudio/managed-gitops/tests-e2e/core.test
github.com/redhat-appstudio/managed-gitops/appstudio-controller/controllers/appstudio.redhat.com
github.com/redhat-appstudio/application-service/pkg/devfile
github.com/devfile/registry-support/registry-library/library
github.com/containerd/containerd/remotes/docker

...
```

For all modules reported by listdeps\_all and not by listdeps\_threedot, the import path
contains a `*.test` package - that should mean the module is a test-only dependency.

[controller-runtime-replace]: https://github.com/redhat-appstudio/managed-gitops/blob/588d1d2f204c537e89416ffc2cec5e9ea51297eb/backend/go.mod#L112
[fd-find]: https://github.com/sharkdp/fd
[go-build-constraints]: https://pkg.go.dev/go/build#hdr-Build_Constraints
