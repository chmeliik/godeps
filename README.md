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

## Module identification methods

See [src/godeps.py](src/godeps.py) for more details on how they're implemented.

* download = `go mod download -json`
* gomodcache = `$GOMODCACHE/cache/download/**/*.zip`
* vendor = parse `vendor/modules.txt`
* vendor\_with\_unused = parse `vendor/modules.txt`, keep modules that don't have packages
* listdeps\_all = `go list -deps -json all`
* listdeps\_threedot = `go list -deps -json ./...`

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

## Interesting findings

Uses [fd][fd-find] as a more convenient `find` replacement.

Prepare data for experimentation:

```shell
# write results to all module directories
fd go.mod -x godeps -m {//} -o {//} -c {//}/.gocache
fd go.mod -x godeps -m {//} -o {//} -c {//}/.gocache --vendor
```

### The relationship between the module identification methods

Check (for all modules at the same time) with:

```shell
fd go.mod -x diff --color=always {//}/<method1>.txt {//}/<method2>.txt
```

When the modules are properly tidied (`go mod tidy`) and there are no local replacements:

```text
download == gomodcache == vendor_with_unused == vendor >= listdeps_all >= listdeps_threedot
```

When the module is not tidied (some modules in `go.mod` are not actually needed), the `vendor`
method notices the not-needed modules. Note that the `vendor` method is consistent with the
actual content of the `vendor/` directory.

```text
download == gomodcache == vendor_with_unused > vendor >= listdeps_all >= listdeps_threedot
```

When there are local replacements, they're not listed either by `download` or by `gomodcache`.
They are, with varying levels of reliability, listed by the others.

### The difference between \<a reliable method\> and listdeps\_all

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

## Conclusion

### Module identification methods vs. corner cases

*See [module identification methods](#module-identification-methods)*

method X lists modules that are...

|                      | build-constrained | test-only | local   | untidy |
|--------------------- | ----------------- | --------- | ------- | ------ |
| download             | ✅                | ✅        | ❌      | ✅     |
| gomodcache           | ✅                | ✅        | ❌      | ✅     |
| vendor               | ✅                | ✅        | ✅      | ❌     |
| vendor\_with\_unused | ✅                | ✅        | ✅      | ✅     |
| listdeps\_all        | ❌                | ✅        | ✅      | ❌     |
| listdeps\_threedot   | ❌                | ❌        | ✅      | ❌     |

* build-constrained = only required for a specific combination of build tags
  * see [interesting finding 2](#the-difference-between-a-reliable-method-and-listdeps_all)
* test-only = only required for tests
  * see [interesting finding 3](#the-difference-between-listdeps_all-and-listdeps_threedot)
* local = `replace module/name => ./local/path` in go.mod
* untidy = `go mod tidy` would remove this module

Note: the method lists the module if NONE of the negative conditions apply. For example,
if a module is local and build-constrained, the `listdeps_all` method will not list it.

### Matching what we download

The `download` method always lists the same modules as `gomodcache`. *N.B. tidiness: when
the module is not tidy, the untidy module does get downloaded and we should report it.*

The `vendor` method matches the modules present in `vendor/`. *N.B. tidiness: when
the module is not tidy, the untidy module __does not__ get vendored and we __should not__
report it.*

### Crafting a universally reliable method

If the user wants to use vendoring, the `vendor` method is 100% perfect.

If not, there are four options.

* `download` + `listdeps_all`
  * hope that none of the locally replaced modules are excluded by build constraints
* `download` + `vendor`
  * but make sure to remove the leftover `vendor/` directory afterwards
* `download` + pick out locally replaced modules from `go list -m all`
  * but `go list -m all` downloads some extra junk (`*.info` files for unneeded modules)
  * and it lists false positives even for local modules /facepalm
* `vendor_with_unused`
  * happens to match `download` even in untidy cases
  * but make sure to remove the leftover `vendor/` directory afterwards

[controller-runtime-replace]: https://github.com/redhat-appstudio/managed-gitops/blob/588d1d2f204c537e89416ffc2cec5e9ea51297eb/backend/go.mod#L112
[fd-find]: https://github.com/sharkdp/fd
[go-build-constraints]: https://pkg.go.dev/go/build#hdr-Build_Constraints
